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

import yaml
import pkg_resources
import docker
import docker.errors
import mako.template
import subprocess
import difflib

from .. import utils


class BaseShip:
    """
    Base mixin class for Ships.
    """
    def __lt__(self, other):
        return self.fqdn < other.fqdn

    def __repr__(self):
        return '{}(name={})'.format(type(self).__name__, self.name)

    @property
    def logger(self):
        return utils.getlogger()

    @property
    def fullname(self):
        return self.name


class Ship(BaseShip):
    """
    Ship objects represents host running Docker listening on 4243 external port.
    """
    def __init__(self, name, fqdn, username='root', datadir='/var/lib/dominator/data',
                 configdir='/var/lib/dominator/config', port=2375, **kwargs):
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
        self.logger.debug("ssh'ing to ship", fqdn=self.fqdn)
        import openssh_wrapper
        conn = openssh_wrapper.SSHConnection(self.fqdn, login=self.username)
        return conn

    def upload(self, localpath, remotepath):
        """Upload directory recursively to ship using ssh
        """
        self.logger.debug("uploading from %s to %s", localpath, remotepath)
        ssh = self.getssh()
        ret = ssh.run('rm -rf {0} && mkdir -p {0}'.format(remotepath))
        if ret.returncode != 0:
            raise RuntimeError(ret.stderr)
        ssh.scp([os.path.join(localpath, entry) for entry in os.listdir(localpath)], remotepath)

    def download(self, remotepath, localpath):
        """Download directory recursively from ship using ssh
        """
        self.logger.debug("downloading from %s to %s", remotepath, localpath)
        ssh = self.getssh()
        tar = ssh.run('tar -cC {} .'.format(remotepath)).stdout
        subprocess.check_output('tar -x -C {}'.format(localpath), input=tar, shell=True)

    def spawn(self, command):
        ssh = self.getssh()
        sshcommand = ssh.ssh_command(command, forward_ssh_agent=False)
        sshcommand.insert(1, b'-t')
        i = utils.PtyInterceptor()
        i.spawn(sshcommand)

    def restart(self):
        ssh = self.getssh()
        self.logger.debug("restarting docker service")
        ssh.run('restart docker')


class LocalShip(BaseShip):
    def __init__(self):
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
        return utils.settings.get('localship-fqdn', 'localhost')

    @property
    def islocal(self):
        return True

    @property
    @utils.cached
    def memory(self):
        import psutil
        return psutil.virtual_memory().total

    @property
    @utils.cached
    def docker(self):
        return docker.Client(utils.settings.get('docker.url'))

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

    def spawn(self, command):
        i = utils.PtyInterceptor()
        i.spawn(['bash', '-c', command])


DEFAULT_NAMESPACE = object()
DEFAULT_REGISTRY = object()


class Image:
    def __init__(self, repository: str, tag: str='latest', id: str=None,
                 namespace=DEFAULT_NAMESPACE, registry=DEFAULT_REGISTRY):
        self.tag = tag
        self._init(namespace, repository, registry, id)
        if self.id is None:
            self.getid()

    def _init(self, namespace, repository, registry, id=None):
        self.id = id
        self.repository = repository
        self.namespace = namespace if namespace is not DEFAULT_NAMESPACE else utils.settings.get('docker.namespace')
        self.registry = registry if registry is not DEFAULT_REGISTRY else utils.settings.get('docker.registry.url',
                                                                                             default=None)

    def __repr__(self):
        return '{classname}({namespace}/{repository}:{tag:.7} [{id:.7}] registry={registry})'.format(
            classname=type(self).__name__, namespace=self.namespace, repository=self.repository, tag=self.tag,
            id=self.id or '-', registry=self.registry)

    def getfullrepository(self):
        registry = (self.registry + '/') if self.registry else ''
        namespace = (self.namespace + '/') if self.namespace else ''
        return registry + namespace + self.repository

    @property
    def logger(self):
        return utils.getlogger()

    def getid(self, dock=None):
        self.logger.debug('retrieving id')
        if self.id is None and self.tag is not None:
            self.id = self.gettags(dock).get(self.tag)
        return self.id

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
        self.getid()

    def build(self, dock=None, **kwargs):
        self.logger.info("building image")
        dock = dock or utils.getdocker()
        self._streamoperation(dock.build, tag='{}:{}'.format(self.getfullrepository(), self.tag), **kwargs)
        Image.gettags.cache_clear()
        self.id = None
        self.getid()

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
        if 'config' in result:
            return result['config']
        elif 'Config' in result:
            return result['Config']
        else:
            raise RuntimeError("unexpected response from Docker: {}".format(result))

    @utils.cached
    def getports(self):
        return [int(port.split('/')[0]) for port in self.inspect()['ExposedPorts'].keys()]

    def getcommand(self):
        return ' '.join(self.inspect()['Cmd'])

    def getenv(self):
        return dict(var.split('=', 1) for var in self.inspect()['Env'])

    def gethash(self):
        self.logger.debug("generating hashtag")
        return '{}:{}[{}]'.format(self.getfullrepository(), self.tag, self.getid())


class SourceImage(Image):
    def __init__(self, name: str, parent: Image, scripts: list=None, command: str=None, workdir: str=None,
                 env: dict=None, volumes: dict=None, ports: dict=None, files: dict=None, user: str=''):
        self.parent = parent
        self.scripts = scripts or []
        self.command = command
        self.workdir = workdir
        self.volumes = volumes or {}
        self.ports = ports or {}
        self.files = files or {}
        self.env = env or {}
        self.user = user
        self._init(namespace=DEFAULT_NAMESPACE, repository=name, registry=DEFAULT_REGISTRY)
        self.tag = self.gethash()

    def build(self, dock=None, **kwargs):
        self.logger.info("building source image")
        if isinstance(self.parent, SourceImage):
            self.parent.build(dock, **kwargs)
        return Image.build(self, dock, fileobj=self.gettarfile(), custom_context=True, **kwargs)

    def gethash(self):
        """Used to calculate unique identifying tag for image
           If tag is not found in registry, than image must be rebuilt
        """
        def serialize_bytes(data):
            return base64.b64encode(data, altchars=b'+-').decode()

        dump = json.dumps({
            'repository': self.repository,
            'namespace': self.namespace,
            'parent': self.parent.gethash(),
            'scripts': self.scripts,
            'command': self.command,
            'workdir': self.workdir,
            'env': self.env,
            'volumes': self.volumes,
            'ports': self.ports,
            'files': self.files,
            'user': self.user,
        }, sort_keys=True, default=serialize_bytes)
        digest = hashlib.sha256(dump.encode()).digest()
        return base64.b64encode(digest, altchars=b'+-').decode()

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
            for volume in self.volumes.values():
                dockerfile.write('VOLUME {}\n'.format(volume).encode())
            for port in self.ports.values():
                dockerfile.write('EXPOSE {}\n'.format(port).encode())
            if self.user:
                dockerfile.write('USER {}\n'.format(self.user).encode())
            if self.command:
                dockerfile.write('CMD {}\n'.format(self.command).encode())
            for path, data in self.files.items():
                dockerfile.write('ADD {} {}\n'.format(path, path).encode())
                tinfo = tarfile.TarInfo(path)
                if isinstance(data, str):
                    data = data.encode()
                if isinstance(data, bytes):
                    data = io.BytesIO(data)
                tinfo.size = len(data.getvalue())
                data.seek(0)
                tfile.addfile(tinfo, data)
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
                 memory: int=0, volumes: dict=None, env: dict=None, doors: dict=None,
                 network_mode: str='', user: str='', privileged: bool=False):
        self.name = name
        self.ship = ship
        self.image = image
        self.command = command
        self.volumes = volumes or {}
        self.memory = memory
        self.env = env or {}
        self.id = None
        self.status = 'not found'
        self.hostname = hostname or '{}-{}'.format(self.name, self.ship.name)
        self.network_mode = network_mode
        self.user = user
        self.privileged = privileged
        self.doors = doors or {}

    def __repr__(self):
        return 'Container({c.fullname}[{c.id!s:7.7}])'.format(c=self)

    def __getstate__(self):
        state = vars(self)
        # id and status are temporary fields and should not be saved
        state['id'] = None
        state['status'] = None
        return state

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
        return '{}:{}'.format(self.ship.name, self.name)

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
            self.id = cinfo.get('Id', self.id)
            self.status = cinfo.get('Status', self.status)
        else:
            self.id = None
            self.status = 'not found'

    @contextlib.contextmanager
    def execute(self):
        self.logger.debug('executing')
        try:
            try:
                self.create()
            except docker.errors.APIError as e:
                if e.response.status_code != 409:
                    raise
                self.check()
                self.remove(force=True)
                self.create()

            self.logger.debug('attaching to stdout/stderr')
            logs = utils.docker_lines(self.ship.docker.attach(
                self.id, stdout=True, stderr=True, logs=True, stream=True))
            self.start()
            yield logs
        finally:
            try:
                self.stop()
            except:
                self.logger.debug('could not stop container, ignoring')

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
            if b'Driver devicemapper failed to remove root filesystem' in e.explanation:
                self.logger.warning("Docker bug 'Driver devicemapper failed to remove root filesystem'"
                                    " detected, just trying again")
                self.check()
                if self.id:
                    self.ship.docker.remove_container(self.id, force=force)
            else:
                raise
        self.check({'Id': None, 'Status': 'not found'})

    def create(self):
        self.logger.debug('preparing to create container')

        for volume in self.volumes.values():
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
                if not re.search('HTTP code: 404', str(e)) and not re.search('Tag .* not found in repository', str(e)):
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
            hostname=self.hostname,
            command=self.command,
            mem_limit=self.memory,
            environment=self.env,
            name=self.dockername,
            ports=[(door.port, door.protocol) for door in self.doors.values()],
            stdin_open=True,
            detach=False,
            user=self.user,
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
                    door.portspec: ('::', door.externalport)
                    for door in self.doors.values()
                    if door.exposed
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

    def getport(self, name):
        """DEPRECATED"""
        return self.doors[name].externalport


class Task(Container):
    pass


class Door:
    """Door class represents an interface to a container - like Docker port, but with additional
    attributes
    """
    def __init__(self, schema, port=None, protocol='tcp', exposed=True, externalport=None, paths=None):
        """
        schema - something like http, ftp, zookeeper, gopher
        port - port number, by default it's deducted from schema (80 for http, 2121 for zookeper)
        protocol - tcp or udp, by default tcp
        exposed - expose or not interface for external access
        externalport - if exposed==True, then map internal port to it
        paths - paths to append to url (by default only '/')
        """
        self.schema = schema
        self.protocol = protocol
        self.port = port if port else socket.getservbyname(schema, protocol)
        self.exposed = exposed
        self.externalport = externalport if externalport else self.port
        self.paths = paths if paths is not None else ['/']

    @property
    @utils.aslist
    def urls(self):
        for path in self.paths:
            yield '{schema}://{fqdn}:{extport}{path}'.format(
                schema=self.schema,
                fqdn=self.container.ship.fqdn,
                extport=self.externalport,
                path=path,
            )

    @property
    def portspec(self):
        return '{port}/{protocol}'.format(port=self.port, protocol=self.protocol)

    @property
    def fullname(self):
        return '{}:{}:{}'.format(self.container.ship.name, self.container.name, self.name)


class Volume:
    def __repr__(self):
        return '{}(fullname={self.fullname}, dest={self.dest})'.format(type(self).__name__, self=self)

    @property
    def logger(self):
        return utils.getlogger()

    @property
    def fullname(self):
        return '{}:{}:{}'.format(self.container.ship.name, self.container.name, self.name)


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
    def __init__(self, text: str=None, filename: str=None):
        """
        Constructs TextFile. If text provided, populate
        file contents from it. If not - try to load resource
        from calling module using filename.
        """
        assert(filename is not None or text is not None)
        if text is not None:
            self.content = text
        else:
            self.content = pkg_resources.resource_string(utils.getcallingmodule(1).__name__, filename).decode()

    @property
    def data(self):
        return self.content


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
        return yaml.dump(self.content)


class JsonFile(BaseFile):
    def __init__(self, data: dict):
        self.content = data

    @property
    def data(self):
        return json.dumps(self.content, sort_keys=True, indent='  ')


class Shipment:
    def __init__(self, name, containers, tasks=None):
        self.name = name
        self.tasks = tasks or []
        ships = {container.ship for container in containers}.union({task.ship for task in self.tasks})
        for ship in ships:
            ship.containers = {container.name: container for container in containers if container.ship == ship}
        self.ships = {ship.name: ship for ship in ships}

    @property
    def containers(self):
        for ship in self.ships.values():
            yield from ship.containers.values()

    @property
    def volumes(self):
        for container in self.containers:
            yield from container.volumes.values()

    @property
    def files(self):
        for volume in self.volumes:
            yield from getattr(volume, 'files', {}).values()

    @property
    def doors(self):
        for container in self.containers:
            yield from container.doors.values()

    def make_backrefs(self):
        def make_backrefs(obj, refname, backrefname):
            ref = getattr(obj, refname)
            for name, child in ref.copy().items():
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

        make_backrefs(self, 'ships', 'shipment')

        for ship in self.ships.values():
            make_backrefs(ship, 'containers', 'ship')

        for container in itertools.chain(self.containers, self.tasks):
            make_backrefs(container, 'doors', 'container')
            make_backrefs(container, 'volumes', 'container')

            for volume in container.volumes.values():
                if hasattr(volume, 'files'):
                    make_backrefs(volume, 'files', 'volume')

    @property
    def images(self):
        def compare_source_images(x, y):
            if isinstance(x, SourceImage):
                if x.parent is y:
                    return 1
            if isinstance(y, SourceImage):
                if x is y.parent:
                    return -1
            return 0

        def iterate_images():
            for container in itertools.chain(self.containers, self.tasks):
                image = container.image
                while True:
                    yield image
                    if isinstance(image, SourceImage):
                        image = image.parent
                    else:
                        break

        return sorted(list(set(iterate_images())), key=functools.cmp_to_key(compare_source_images))


class LogFile(BaseFile):
    def __init__(self, format='', length=None):
        if length is None:
            length = len(datetime.datetime.strftime(datetime.datetime.now(), format))
        self.length = length
        self.format = format


class RotatedLogFile(LogFile):
    pass
