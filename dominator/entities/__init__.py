"""
This module contains classes used to describe cluster configuration
"""


import os.path
import os
import json
import contextlib
import tarfile
import hashlib
import tempfile
import base64
import io
import functools
import re
import shutil
import datetime
import socket
import copy
import itertools
import logging
import shlex
import sys

import yaml
import docker
import docker.errors
import mako.template
import subprocess
import difflib

from .. import utils


class BaseShip:
    """
    Base class for Ships.
    """
    def __init__(self):
        self.containers = {}

    def __lt__(self, other):
        return self.fqdn < other.fqdn

    def __repr__(self):
        return '{}(name={})'.format(type(self).__name__, self.name)

    def make_backrefs(self):
        make_backrefs(self, 'containers', 'ship')

    @property
    def logger(self):
        return utils.getlogger()

    @property
    def fullname(self):
        return self.name

    def info(self):
        return self.docker.info()

    def place(self, container):
        """Place the container on the ship."""
        assert container.name not in self.containers, "container {} already loaded on the ship".format(container.name)
        self.containers[container.name] = container
        self.make_backrefs()

    def expose_ports(self, port_range):
        assert port_range.stop < 65536, "Port range end exceeds 65535"
        ports = list(port_range)
        for _, container in sorted(self.containers.items()):
            for _, door in sorted(container.doors.items()):
                if not door.exposed:
                    port = ports.pop()
                    door.expose(port)


class Ship(BaseShip):
    """
    Ship objects represents host running Docker listening on 4243 external port.
    """
    def __init__(self, name, fqdn, username='root', datadir='/var/lib/dominator/data',
                 configdir='/var/lib/dominator/config', port=2375, **kwargs):
        super().__init__()
        self.name = name
        self.fqdn = fqdn
        self.port = port
        self.datadir = datadir
        self.configdir = configdir
        self.username = username
        for k, v in kwargs.items():
            setattr(self, k, v)

    @property
    @utils.cached
    def islocal(self):
        return self.name == os.uname()[1]

    @property
    @utils.cached
    def docker(self):
        self.logger.debug('connecting to docker api on ship', fqdn=self.fqdn)
        return docker.Client('http://{}:{}'.format(self.fqdn, self.port))

    @utils.cached
    def getssh(self):
        self.logger.debug("ssh'ing to ship", fqdn=self.fqdn, login=self.username)
        import openssh_wrapper
        conn = openssh_wrapper.SSHConnection(self.fqdn, login=self.username)
        return conn

    def upload(self, localpath, remotepath):
        """Upload directory recursively to ship using ssh
        """
        self.logger.debug("uploading from %s to %s", localpath, remotepath)
        ssh = self.getssh()
        ret = ssh.run('rm -rf {0} && mkdir -p {0}'.format(remotepath))
        assert ret.returncode == 0, "command execution failed (retcode={}): {}".format(ret.returncode, ret.stderr)
        ssh.scp([os.path.join(localpath, entry) for entry in os.listdir(localpath)], remotepath)

    def download(self, remotepath, localpath):
        """Download directory recursively from ship using ssh
        """
        self.logger.debug("downloading from %s to %s", remotepath, localpath)
        ssh = self.getssh()
        tar = ssh.run('tar -cC {} .'.format(remotepath)).stdout
        subprocess.check_output('tar -x -C {}'.format(localpath), input=tar, shell=True)

    def spawn(self, command, sudo=False):
        ssh = self.getssh()
        sudocmd = 'sudo ' if sudo else ''
        sshcommand = ssh.ssh_command(sudocmd + command, forward_ssh_agent=False)
        sshcommand.insert(1, b'-t')
        self.logger.debug("executing ssh command", command=sshcommand)
        i = utils.PtyInterceptor()
        i.spawn(sshcommand)

    def restart(self):
        ssh = self.getssh()
        self.logger.debug("restarting docker service")
        ssh.run('restart docker')


class LocalShip(BaseShip):
    def __init__(self):
        super().__init__()
        from OpenSSL import crypto
        k = crypto.PKey()
        k.generate_key(crypto.TYPE_RSA, 1024)
        cert = crypto.X509()
        cert.set_serial_number(1000)
        cert.gmtime_adj_notBefore(0)
        cert.gmtime_adj_notAfter(315360000)
        cert.set_pubkey(k)
        cert.sign(k, 'sha1')
        certpem = crypto.dump_certificate(crypto.FILETYPE_PEM, cert).decode()
        keypem = crypto.dump_privatekey(crypto.FILETYPE_PEM, k).decode()
        self.certificate = certpem + keypem

    @property
    def datacenter(self):
        return 'localdc'

    @property
    def name(self):
        return 'localship'

    @property
    def fqdn(self):
        return utils.settings.get('localship.fqdn', 'localhost')

    @property
    def islocal(self):
        return True

    @property
    @utils.cached
    def memory(self):
        try:
            return utils.settings.get('localship.memory')
        except utils.NoSuchSetting:
            import psutil
            return psutil.virtual_memory().total

    @property
    @utils.cached
    def docker(self):
        return docker.Client(utils.settings.get('docker.url', None))

    @property
    def datadir(self):
        return utils.settings['datavolumedir']

    @property
    def configdir(self):
        return utils.settings['configvolumedir']

    def upload(self, localpath, remotepath):
        """Upload directory recursively to localship using shutil
        """
        shutil.rmtree(remotepath, ignore_errors=True)
        shutil.copytree(localpath, remotepath)

    def download(self, remotepath, localpath):
        """Download directory recursively from localship using shutil
        """
        shutil.rmtree(localpath, ignore_errors=True)
        shutil.copytree(remotepath, localpath)

    def spawn(self, command, sudo=False):
        i = utils.PtyInterceptor()
        sudocmd = ['sudo'] if sudo else []
        if isinstance(command, str):
            command = command.split(' ')
        i.spawn(sudocmd + command)

    def restart(self):
        pass


DEFAULT_NAMESPACE = object()
DEFAULT_REGISTRY = object()


class BaseImage:
    def __init__(self, namespace, repository, registry):
        self.repository = repository

        if namespace is DEFAULT_NAMESPACE:
            namespace = utils.settings.get('docker.namespace', default=None)
        self.namespace = namespace

        if registry is DEFAULT_REGISTRY:
            registry = utils.settings.get('docker.registry.url', default=None)
        self.registry = registry

    def __repr__(self):
        return '{classname}({namespace}/{repository}:{tag:.7} registry={registry})'.format(
            classname=type(self).__name__, namespace=self.namespace, repository=self.repository, tag=self.tag,
            registry=self.registry)

    def getfullrepository(self):
        registry = (self.registry + '/') if self.registry else ''
        namespace = (self.namespace + '/') if self.namespace else ''
        return registry + namespace + self.repository

    @property
    def logger(self):
        return utils.getlogger()

    @utils.cached
    def getid(self):
        self.logger.debug('retrieving id')
        imageid = self.gettags(None).get(self.tag)
        if imageid is None:
            self.logger.warning("could not find tag for image", tag=self.tag)
        return imageid

    def _streamoperation(self, func, **kwargs):
        with utils.addcontext(image=self, docker=func.__self__, operation=func.__name__):
            logger = logging.getLogger('dominator.docker.'+func.__name__)
            for line in func(stream=True, **kwargs):
                if line != '':
                    resp = json.loads(line)
                    if 'error' in resp:
                        raise docker.errors.DockerException('could not complete {} operation on {} ({})'.format(
                            func.__name__, self, resp['error']))
                    else:
                        message = resp.get('stream', resp.get('status', ''))
                        for line in message.split('\n'):
                            if line:
                                logger.debug(line, response=resp)

    def push(self, dock=None):
        self.logger.info("pushing repo")
        dock = dock or utils.getdocker()
        self._streamoperation(dock.push, repository=self.getfullrepository(), tag=self.tag,
                              insecure_registry=utils.settings.get('docker.registry.insecure', False))

    def pull(self, dock=None, tag=None):
        self.logger.info("pulling repo")
        dock = dock or utils.getdocker()
        self._streamoperation(dock.pull, repository=self.getfullrepository(), tag=tag,
                              insecure_registry=utils.settings.get('docker.registry.insecure', False))
        Image.gettags.cache_clear()
        Image.getid.cache_clear()

    def build(self, dock=None, **kwargs):
        self.logger.info("building image")
        dock = dock or utils.getdocker()
        self._streamoperation(dock.build, tag='{}:{}'.format(self.getfullrepository(), self.tag), **kwargs)
        Image.gettags.cache_clear()
        Image.getid.cache_clear()

    @utils.cached
    @utils.asdict
    def gettags(self, dock):
        self.logger.debug("retrieving tags")
        dock = dock or utils.getdocker()
        images = dock.images(self.getfullrepository(), all=True)
        for image in images:
            for tag in image['RepoTags']:
                yield tag.split(':')[-1], image['Id']

    def inspect(self):
        result = utils.getdocker().inspect_image(self.getid())
        # Workaround: Docker sometimes returns "config" key in different casing
        assert 'config' in result or 'Config' in result, "unexpected response from Docker"
        return result['config'] if 'config' in result else result['Config']

    @utils.cached
    def getports(self):
        return [int(port.split('/')[0]) for port in self.inspect()['ExposedPorts'].keys()]

    def getcommand(self):
        return ' '.join(self.inspect()['Cmd'])

    def getenv(self):
        return dict(var.split('=', 1) for var in self.inspect()['Env'])


class Image(BaseImage):
    def __init__(self, repository: str, tag: str='latest',
                 namespace=DEFAULT_NAMESPACE, registry=DEFAULT_REGISTRY):
        super().__init__(namespace, repository, registry)
        self.tag = tag


def convert_fileobj(path, fileobj_or_data):
    """Converts <fileobj-or-data>} to a tuple (tarinfo, data)"""
    tfile = tarfile.TarFile(fileobj=io.BytesIO(), mode='w')
    if hasattr(fileobj_or_data, 'fileno'):
        fileobj = fileobj_or_data
        tinfo = tfile.gettarinfo(arcname=path, fileobj=fileobj)
        data = fileobj.read()
        fileobj.seek(0)
    else:
        data = fileobj_or_data
        tinfo = tarfile.TarInfo(path)
        if isinstance(data, str):
            tinfo.size = len(data.encode())
        elif isinstance(data, bytes):
            tinfo.size = len(data)
        else:
            raise ValueError("Data should be str, bytes or file-like object, not {}".format(type(data)))
    if isinstance(data, bytes):
        with contextlib.suppress(UnicodeDecodeError):
            data = data.decode()
    # Zero some fields to make info independend from host
    tinfo.gid = 0
    tinfo.gname = 'root'
    tinfo.uid = 0
    tinfo.uname = 'root'
    tinfo.mtime = 0.0

    return tinfo.get_info(), data


class SourceImage(BaseImage):
    def __init__(self, name: str, parent: Image, scripts: list=None, command: str=None, workdir: str=None,
                 env: dict=None, volumes: dict=None, ports: dict=None, files: dict=None, user: str='',
                 entrypoint=None):
        self.parent = parent
        self.scripts = scripts or []
        self.command = command
        self.workdir = workdir
        self.volumes = volumes or {}
        self.ports = ports or {}
        self.files = {}
        self.env = env or {}
        self.user = user
        self.entrypoint = entrypoint
        self.files = {path: convert_fileobj(path, fileobj_or_data) for path, fileobj_or_data in (files or {}).items()}
        super().__init__(namespace=DEFAULT_NAMESPACE, repository=name, registry=DEFAULT_REGISTRY)

    def build(self, dock=None, **kwargs):
        self.logger.info("building source image")
        if isinstance(self.parent, SourceImage):
            self.parent.build(dock, **kwargs)
        return Image.build(self, dock, fileobj=self.gettarfile(), custom_context=True, **kwargs)

    @property
    def tag(self):
        """Calculate tag for image from it's attributes"""
        dump = yaml.dump(self)
        digest = hashlib.sha256(dump.encode()).digest()
        return base64.b64encode(digest, altchars=b'_-').decode().replace('=', '.')

    def gettarfile(self):
        f = tempfile.NamedTemporaryFile()
        with tarfile.open(mode='w', fileobj=f) as tfile:
            dockerfile = io.BytesIO()
            dockerfile.write('FROM {}:{}\n'.format(self.parent.getfullrepository(), self.parent.getid()).encode())
            for name, value in self.env.items():
                dockerfile.write('ENV {} {}\n'.format(name, value).encode())
            if self.workdir is not None:
                dockerfile.write('WORKDIR {}\n'.format(self.workdir).encode())
            for script in self.scripts:
                dockerfile.write('RUN {}\n'.format(script).encode())
            for _, volume in sorted(self.volumes.items()):
                dockerfile.write('VOLUME {}\n'.format(volume).encode())
            for _, port in sorted(self.ports.items()):
                dockerfile.write('EXPOSE {}\n'.format(port).encode())
            if self.user:
                dockerfile.write('USER {}\n'.format(self.user).encode())

            def convert_command(command):
                command = shlex.split(command) if isinstance(command, str) else command
                return json.dumps(command)

            if self.command is not None:
                dockerfile.write('CMD {}\n'.format(convert_command(self.command)).encode())
            if self.entrypoint is not None:
                dockerfile.write('ENTRYPOINT {}\n'.format(convert_command(self.entrypoint)).encode())
            for path, (tinfo, data) in self.files.items():
                dockerfile.write('ADD {} {}\n'.format(path, path).encode())
                if isinstance(data, str):
                    data = data.encode()
                if isinstance(data, bytes):
                    data = io.BytesIO(data)
                else:
                    raise RuntimeError("Could not add {} as file".format(type(data)))
                tarinfo = tarfile.TarInfo()
                for k, v in tinfo.items():
                    setattr(tarinfo, k, v)
                tfile.addfile(tarinfo, data)
            dfinfo = tarfile.TarInfo('Dockerfile')
            dfinfo.size = len(dockerfile.getvalue())
            dockerfile.seek(0)
            tfile.addfile(dfinfo, dockerfile)

        f.seek(0)
        return f

    def getports(self):
        return self.ports


class Container:
    def __init__(self, name: str, image: Image, ship: Ship=None, command: str=None, hostname: str=None,
                 memory: int=0, volumes: dict=None, env: dict=None, doors: dict=None, links: dict=None,
                 network_mode: str='', user: str='', privileged: bool=False, entrypoint: str=None):
        self.name = name
        self.ship = ship
        self.image = image
        self.command = command
        self.volumes = volumes or {}
        self.memory = memory
        self.env = env or {}
        self.id = None
        self.status = 'not found'
        self.hostname = hostname
        self.network_mode = network_mode
        self.user = user
        self.privileged = privileged
        self.entrypoint = entrypoint
        self.doors = doors or {}
        self.links = links or {}
        self.make_backrefs()

    def __repr__(self):
        return '<Container {c.fullname} [{c.id!s:7.7}]>'.format(c=self)

    def __getstate__(self):
        self.logger.debug("serializing state")
        state = vars(self).copy()
        # id and status are temporary fields and should not be saved
        state['id'] = None
        state['status'] = None
        return state

    def make_backrefs(self):
        make_backrefs(self, 'doors', 'container')
        make_backrefs(self, 'volumes', 'container')

    @property
    def logger(self):
        return utils.getlogger()

    def getvolume(self, volumename):
        return self.volumes[volumename]

    @property
    def running(self):
        return 'Up' in self.status

    @property
    def dockername(self):
        return '{}.{}'.format(self.ship.shipment.name, self.name)

    @property
    def fullname(self):
        return '{}:{}'.format(self.ship.name if self.ship else '', self.name)

    def check(self, cinfo=None):
        """This function tries to find container on the associated ship
        by listing all containers. If found, it fills `id` and `status` attrs.
        If `cinfo` is provided, then skips docker api call for container listing.
        """
        if cinfo is None:
            self.logger.debug('checking container status')
            matched = [cont for cont in self.ship.docker.containers(all=True)
                       if cont['Names'] and cont['Names'][0][1:] == self.dockername]
            if len(matched) > 0:
                cinfo = matched[0]

        if cinfo:
            # Custom cinfo could provide only one of (id, state), so
            # we should preserve original value
            self.id = cinfo.get('Id', self.id)
            self.status = cinfo.get('Status', self.status)
        else:
            self.id = None
            self.status = 'not found'

    def create_or_recreate(self):
        try:
            self.create()
        except docker.errors.APIError as e:
            if e.response.status_code != 409:
                raise
            else:
                # Container already exists
                self.check()
                self.remove(force=True)
                self.create()

    @contextlib.contextmanager
    def execute(self):
        self.logger.debug('executing')
        try:
            self.create_or_recreate()
            self.logger.debug('attaching to stdout/stderr')
            if not os.isatty(sys.stdin.fileno()):
                self.logger.debug('attaching stdin')
                stdin = sys.stdin.buffer
            else:
                stdin = None
            logs = utils.docker_lines(self.ship.docker.attach(
                self.id, stdout=True, stderr=True, stdin=stdin, logs=True, stream=True))
            self.start()
            yield logs
        finally:
            with contextlib.suppress(Exception):
                self.stop()

    def exec_with_tty(self):
        self.logger.debug("executing with tty")
        try:
            self.create_or_recreate()
            self.ship.spawn('docker start -ai {}'.format(self.id))
        finally:
            with contextlib.suppress(Exception):
                self.stop()

    def logs(self, follow):
        self.logger.debug('getting logs from container', follow=follow)
        try:
            if follow:
                lines = utils.docker_lines(self.ship.docker.logs(self.id, stream=True))
            else:
                lines = self.ship.docker.logs(self.id).decode().split('\n')
            yield from lines
        except KeyboardInterrupt:
            self.logger.debug('received keyboard interrupt')

    def stop(self):
        self.logger.debug('stopping container')
        self.ship.docker.stop(self.id, timeout=2)
        self.check({'Status': 'stopped'})

    def remove(self, force=False):
        self.logger.debug('removing container')
        try:
            self.ship.docker.remove_container(self.id, force=force)
        except docker.errors.APIError as e:
            if any(re.search(pattern, e.explanation) for pattern in [
                b'Driver devicemapper failed to remove root filesystem',
                b'Unable to remove filesystem for .* directory not empty'
            ]):
                self.logger.warning("Docker bug ({}) detected, just trying again".format(e.explanation.decode()))
                self.check()
                if self.id:
                    self.ship.docker.remove_container(self.id, force=force)
            else:
                raise
        self.check({'Id': None, 'Status': 'not found'})

    def create(self):
        """Try to create container. If image is not found, then try to pull or even push it first."""
        with utils.addcontext(image=self.image):
            self.logger.debug('preparing to create container')

            for _, volume in sorted(self.volumes.items()):
                volume.render(self)

            try:
                cinfo = self._create()
            except docker.errors.APIError as e:
                if e.response.status_code != 404:
                    raise
                # image not found - pull repo and try again
                # Check if ship has needed image
                self.logger.info('could not find requested image, pulling repo')
                try:
                    self.image.pull(self.ship.docker, tag=self.image.tag)
                except docker.errors.DockerException as e:
                    if not any([re.search(pattern, str(e))
                                for pattern in ['HTTP code: 404', 'Tag .* not found in repository']]):
                        raise
                    self.logger.info("could not find requested image in registry, pushing repo")
                    self.image.push()
                    self.image.pull(self.ship.docker)
                cinfo = self._create()

            self.check(cinfo)
            self.logger.debug('container created')

    def _create(self):
        self.logger.debug('creating container', image=self.image)
        return self.ship.docker.create_container(
            image='{}:{}'.format(self.image.getfullrepository(), self.image.getid()),
            hostname=self.hostname or '{}-{}'.format(self.name, self.ship.name),
            command=self.command,
            mem_limit=self.memory,
            environment=self.env,
            name=self.dockername,
            ports=[(door.internalport, door.protocol) for _, door in sorted(self.doors.items())],
            stdin_open=True,
            tty=True,
            detach=False,
            user=self.user,
            entrypoint=self.entrypoint,
        )

    def run(self):
        self.check()
        if self.running:
            self.logger.info('found running container with the same name, comparing config with requested')
            diff = utils.compare_container(self, self.inspect())
            if diff:
                self.logger.info('running container config differs from requested, stopping', diff=diff)
                self.stop()
            else:
                self.logger.info('running container config identical to requested, keeping')
                return

        if self.id:
            self.logger.info('found stopped container with the same name, removing')
            self.remove()

        self.create()
        self.start()

    def start(self):
        self.logger.debug('starting container')

        def _start():
            self.ship.docker.start(
                self.id,
                port_bindings={
                    door.portspec: ('::', door.port)
                    for door in self.doors.values()
                    if door.port is not None
                },
                binds={v.fullpath: {'bind': v.dest, 'ro': v.ro} for v in self.volumes.values()},
                network_mode=self.network_mode,
                privileged=self.privileged,
            )
        try:
            _start()
        except docker.errors.APIError as e:
            if b'Cannot find child for' in e.explanation:
                self.logger.debug('', exc_info=True)
                self.logger.warning("Docker bug 'Cannot find child' detected, waiting 2 seconds and trying "
                                    "to start container again")
                _start()
            elif b'port has already been allocated' in e.explanation:
                self.logger.debug('', exc_info=True)
                self.logger.error("Docker bug 'port has already been allocated' detected, try to restart "
                                  "Docker manually")
                return
            else:
                raise

        self.check({'Status': 'Up'})
        self.logger.debug('container started')

    def inspect(self):
        return self.ship.docker.inspect_container(self.id)

    def wait(self):
        return self.ship.docker.wait(self.id)

    def expose_all(self, offset=0):
        """Expose all container doors' ports with offset."""
        for door in self.doors.values():
            door.expose(door.internalport + offset)

    def enter(self, command):
        """nsenter to container."""
        assert self.running, "Container should run to enter"
        pid = self.inspect()['State']['Pid']
        self.ship.spawn('nsenter --target {pid} --mount --uts --ipc --net --pid'
                        ' -- env --ignore-environment -- {command}'.format(pid=pid, command=command),
                        sudo=True)


class Task(Container):
    pass


class Door:
    """Door class represents an interface to a container - like Docker port, but with additional
    attributes
    """
    def __init__(self, schema, port=None, protocol='tcp', urls=None, sameports=False):
        """
        schema - something like http, ftp, zookeeper, gopher
        port - port number, by default it's deducted from schema (80 for http, 2121 for zookeper)
        protocol - tcp or udp, by default tcp
        urls - urls that are accessible via door
        sameports - if door doesn't support port mapping (like JMX etc.) then set it to true
        """
        self.schema = schema
        self.protocol = protocol
        self.internalport = port if port else socket.getservbyname(schema, protocol)
        self.exposedport = None
        self.sameports = sameports
        self.urls = {'default': Url('')}
        self.urls.update(urls or {})

    def __repr__(self):
        return '<Door {}>'.format(self.fullname)

    def __format__(self, formatspec):
        if hasattr(self, formatspec):
            return str(getattr(self, formatspec))
        if formatspec == 'host':
            return self.container.ship.fqdn
        if formatspec == '':
            return str(self)
        raise RuntimeError('invalid format spec {}'.format(formatspec))

    def make_backrefs(self):
        make_backrefs(self, 'urls', 'door')

    @property
    def portspec(self):
        return '{port}/{protocol}'.format(port=self.internalport, protocol=self.protocol)

    @property
    def fullname(self):
        return '{}:{}:{}'.format(self.container.ship.name, self.container.name, self.name)

    @property
    def host(self):
        return self.container.ship.fqdn

    @property
    def port(self):
        assert self.exposed, "door is not exposed"
        return self.exposedport

    @property
    def exposed(self):
        return self.exposedport is not None

    @property
    def hostport(self):
        return '{host}:{port}'.format(host=self.host, port=self.port)

    def expose(self, port):
        """Make door export (e.g. map port outside the container)."""
        assert port < 65536, "Port should be less than 65536"
        for container in self.container.ship.containers.values():
            for door in container.doors.values():
                assert door.exposedport != port, "Port {} is already exposed on {}".format(port, door)
        self.exposedport = port
        if self.sameports:
            for door in self.container.doors.values():
                assert door.internalport != port, "Port {} is already bound to {}".format(port, door)
            self.internalport = port


class Url:
    def __init__(self, path):
        self.path = path

    def __str__(self):
        return '{schema}://{fqdn}:{port}/{path}'.format(
            schema=self.door.schema,
            fqdn=self.door.container.ship.fqdn,
            port=self.door.exposedport,
            path=self.path
        )


class Volume:
    def __repr__(self):
        return '{}(dest={self.dest})'.format(type(self).__name__, self=self)

    def make_backrefs(self):
        if hasattr(self, 'files'):
            make_backrefs(self, 'files', 'volume')

    @property
    def logger(self):
        return utils.getlogger()

    @property
    def fullname(self):
        return '{}:{}:{}'.format(self.container.ship.name, self.container.name, self.name)

    def erase(self):
        self.container.ship.spawn('rm -rf {}'.format(self.fullpath), sudo=True)


class DataVolume(Volume):
    """DataVolume that just mounts volume inside container.

    dest -- mount point inside container
    path -- mount point on the host
    ro   -- should volume be mounted read-only
    """
    def __init__(self, dest: str, path: str=None, ro=False):
        self.dest = dest
        self.path = path
        self.ro = ro

    def render(self, _):
        pass

    @property
    def fullpath(self):
        return self.path or os.path.expanduser(os.path.join(self.container.ship.datadir,
                                               self.container.ship.shipment.name, self.container.name, self.dest[1:]))


class LogVolume(DataVolume):
    def __init__(self, dest: str=None, path: str=None, files=None):
        DataVolume.__init__(self, dest, path)
        self.files = files or {}


class ConfigVolume(Volume):
    def __init__(self, dest: str, files: dict=None):
        self.dest = dest
        self.files = files or {}
        self.make_backrefs()

    def __getstate__(self):
        """Replace all "closure-files" with invokation result."""
        for name, file in self.files.items():
            if callable(file):
                self.files[name] = file()
        self.make_backrefs()
        return vars(self)

    @property
    def fullpath(self):
        return os.path.expanduser(os.path.join(self.container.ship.configdir,
                                               self.container.ship.shipment.name, self.container.name, self.dest[1:]))

    @property
    def ro(self):
        return True

    def render(self, container):
        self.logger.debug('rendering')
        with tempfile.TemporaryDirectory() as tempdir:
            for name, file in self.files.items():
                file.dump(os.path.join(tempdir, name))
            container.ship.upload(tempdir, self.fullpath)

    @utils.aslist
    def compare_files(self):
        self.logger.debug('comparing files')
        with tempfile.TemporaryDirectory() as tempdir:
            self.container.ship.download(self.fullpath, tempdir)
            for name, file in self.files.items():
                self.logger.debug("comparing file", file=file)
                try:
                    actual = file.load(os.path.join(tempdir, name))
                    expected = file.data
                    if actual != expected:
                        diff = difflib.Differ().compare(actual.split('\n'), expected.split('\n'))
                        yield ('volumes', self.dest, 'files', name), [line for line in diff if line[:2] != '  ']
                except FileNotFoundError:
                    yield ('volumes', self.dest, 'files'), (name, '<not found>')


class BaseFile:
    def __str__(self):
        return '{}({:40.40})'.format(type(self).__name__, self.fullname)

    @property
    def fullname(self):
        return '{}:{}'.format(self.volume.fullname, self.name)

    @property
    def fullpath(self):
        return os.path.join(self.volume.fullpath, self.name)

    @property
    def fulldest(self):
        return os.path.join(self.volume.dest, self.name)

    @property
    def logger(self):
        return utils.getlogger()

    def dump(self, path: str):
        self.logger.debug("writing file", path=path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w+', encoding='utf8') as f:
            f.write(self.data)

    def load(self, path: str):
        self.logger.debug("loading text file contents", path=path)
        with open(path) as f:
            return f.read()


class TextFile(BaseFile):
    def __init__(self, text: str):
        self.data = text


class TemplateFile(BaseFile):
    def __init__(self, template: str, **context):
        self.template = template
        self.context = context

    def __str__(self):
        return 'TemplateFile(fullname={self.fullname}, context={self.context!s:60.60})'.format(self=self)

    @property
    def data(self):
        template = mako.template.Template(self.template)
        context = {'this': self.volume.container}
        context.update(self.context)
        self.logger.debug('rendering template file', context=context)
        return template.render(**context)


class YamlFile(BaseFile):
    def __init__(self, data: dict):
        self.content = data

    @property
    def data(self):
        return yaml.dump(self.content, default_flow_style=False)


class JsonFile(BaseFile):
    def __init__(self, data: dict):
        self.content = data

    @property
    def data(self):
        return json.dumps(self.content, sort_keys=True, indent='  ')


class IniFile(BaseFile):
    def __init__(self, data: dict):
        self.content = data

    @property
    def data(self):
        return '\n'.join(sorted(['{}={}'.format(key, value) for key, value in self.content.items()]))


def make_backrefs(obj, refname, backrefname):
    """Make links from each "refname" child back to obj,
    then repeat for each child
    """
    ref = getattr(obj, refname)
    for name, child in ref.copy().items():
        if callable(child) or child is None:
            continue
        backref = getattr(child,  backrefname, None)
        if backref is obj:
            continue
        if backref is not None:
            # If single child object is shared between parents, then
            # we should create copy of it to not override backref attr
            # in the shared child object
            ref[name] = child = copy.copy(child)
            # Additionally, copy all list/dict/set attributes to ensure
            # that they will not be shared between objects.
            # We do not use copy.deepcopy here because it's too slow
            # for even medium-sized (hundreds of objects) graphs if there are
            # cyclic references (as in this case).
            for attrname in vars(child):
                attr = getattr(child, attrname)
                if isinstance(attr, (list, dict, set)):
                    setattr(child, attrname, copy.copy(attr))
        setattr(child, backrefname, obj)
        if getattr(child, 'name', None) is None:
            setattr(child, 'name', name)

        if hasattr(child, 'make_backrefs'):
            child.make_backrefs()


class InvalidShipmentFile(Exception):
    pass


class Shipment:
    def __init__(self, name='unnamed', ships=None, tasks=None):
        self.name = name
        self.tasks = tasks or {}
        self.ships = ships or {}
        self.make_backrefs()

    def unload_ships(self):
        """Unload all containers from all ships."""
        for ship in self.ships.values():
            ship.containers.clear()

    @staticmethod
    def load(filename):
        utils.getlogger().debug("loading shipment", shipment_filename=filename)
        with open(filename, 'r') as file:
            shipment = yaml.load(file)
            if not isinstance(shipment, Shipment):
                raise InvalidShipmentFile("Shipment file should consist serialized Shipment object only")
            shipment.filename = filename
        if shipment.dominator_version != utils.getversion():
            utils.getlogger().warning("current dominator version {} do not match shipment version {}".format(
                utils.getversion(), shipment.dominator_version))
        return shipment

    def save(self):
        utils.getlogger().debug("saving shipment", shipment_filename=self.filename)
        self.dominator_version = utils.getversion()

        import tzlocal
        self.timestamp = datetime.datetime.now(tz=tzlocal.get_localzone())

        self.make_backrefs()

        dump = yaml.dump(self, default_flow_style=False)
        with open(self.filename, 'w+') as file:
            file.write(dump)

    @property
    def containers(self):
        for _, ship in sorted(self.ships.items()):
            yield from (cont for _, cont in sorted(ship.containers.items()))

    @property
    def volumes(self):
        for container in self.containers:
            yield from (volume for _, volume in sorted(container.volumes.items()))

    @property
    def files(self):
        for volume in self.volumes:
            yield from (file for _, file in sorted(getattr(volume, 'files', {}).items()))

    @property
    def doors(self):
        for container in self.containers:
            yield from (door for _, door in sorted(container.doors.items()))

    def make_backrefs(self):
        make_backrefs(self, 'ships', 'shipment')
        make_backrefs(self, 'tasks', 'shipment')

    @property
    def images(self):
        """Iterates over SourceImages in build order (parent then child etc.)."""
        def compare_source_images(x, y):
            if isinstance(x, SourceImage):
                if x.parent is y:
                    return 1
            if isinstance(y, SourceImage):
                if x is y.parent:
                    return -1
            return 0

        def iterate_images():
            for container in itertools.chain(self.containers, self.tasks.values()):
                image = container.image
                while True:
                    yield image
                    if isinstance(image, SourceImage):
                        image = image.parent
                    else:
                        break

        return sorted(list(set(iterate_images())), key=functools.cmp_to_key(compare_source_images))

    def expose_ports(self, portrange):
        """Expose all ports on all ships."""
        for _, ship in sorted(self.ships.items()):
            ship.expose_ports(portrange)


class LogFile(BaseFile):
    def __init__(self, format='', length=None):
        if length is None:
            length = len(datetime.datetime.strftime(datetime.datetime.now(), format))
        self.length = length
        self.format = format


class RotatedLogFile(LogFile):
    pass
