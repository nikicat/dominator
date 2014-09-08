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

    def containers(self, containers):
        return [c for c in containers if c.ship == self]

    @property
    def logger(self):
        return utils.getlogger(ship=self, bindto=3)


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
        subprocess.check_output('tar -x --one-top-level={}'.format(localpath), input=tar, shell=True)


class LocalShip(BaseShip):
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
        return docker.Client(utils.settings.get('dockerurl'))

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


DEFAULT_NAMESPACE = object()
DEFAULT_REGISTRY = object()


class Image:
    def __init__(self, repository: str, tag: str='latest', id: str='',
                 namespace=DEFAULT_NAMESPACE, registry=DEFAULT_REGISTRY):
        self.tag = tag
        self._init(namespace, repository, registry, id)
        if self.id is '':
            self.getid()

    def _init(self, namespace, repository, registry, id=''):
        self.id = id
        self.repository = repository
        self.namespace = namespace if namespace is not DEFAULT_NAMESPACE else utils.settings.get('docker-namespace')
        self.registry = registry if registry is not DEFAULT_REGISTRY else utils.settings.get('docker-registry')

    def __repr__(self):
        return '{classname}({namespace}/{repository}:{tag} [{id:.7}] registry={registry})'.format(
            classname=type(self).__name__, **vars(self))

    def __getstate__(self):
        if self.id is '':
            raise RuntimeError('image needs to be rebuilt')
        return vars(self)

    def getfullrepository(self):
        registry = self.registry + '/' if self.registry else ''
        namespace = self.namespace + '/' if self.namespace else ''
        return registry + namespace + self.repository

    @property
    def logger(self):
        return utils.getlogger(image=self, bindto=3)

    def getid(self, dock=None):
        self.logger.debug('retrieving id')
        if self.id is '' and self.tag is not '':
            self.id = self.gettags(dock).get(self.tag, '')
        return self.id

    def _streamoperation(self, func, **kwargs):
        logger = utils.getlogger('dominator.docker.{}'.format(func.__name__), image=self, docker=func.__self__)
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
        self._streamoperation(dock.push, repository=self.getfullrepository(), tag=self.tag)

    def pull(self, dock=None, tag=None):
        self.logger.info("pulling repo")
        dock = dock or utils.getdocker()
        self._streamoperation(dock.pull, repository=self.getfullrepository(), tag=tag)
        Image.gettags.cache_clear()
        self.getid()

    def build(self, dock=None, **kwargs):
        self.logger.info("building image")
        dock = dock or utils.getdocker()
        self._streamoperation(dock.build, tag='{}:{}'.format(self.getfullrepository(), self.tag), **kwargs)
        Image.gettags.cache_clear()
        self.id = ''
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
        return '{}:{}[{}]'.format(self.getfullrepository(), self.tag, self.getid())


class SourceImage(Image):
    def __init__(self, name: str, parent: Image, scripts: list=None, command: str=None, workdir: str=None,
                 env: dict=None, volumes: dict=None, ports: dict=None, files: dict=None):
        self.parent = parent
        self.scripts = scripts or []
        self.command = command
        self.workdir = workdir
        self.volumes = volumes or {}
        self.ports = ports or {}
        self.files = files or {}
        self.env = env or {}
        self._init(namespace=DEFAULT_NAMESPACE, repository=name, registry=DEFAULT_REGISTRY)
        self.tag = self.gethash()
        self.getid()

    def build(self, dock=None, **kwargs):
        self.logger.info("building source image")
        if isinstance(self.parent, SourceImage):
            self.parent.build(dock, **kwargs)
        return Image.build(self, dock, fileobj=self.gettarfile(), custom_context=True, **kwargs)

    def gethash(self):
        """Used to calculate unique identifying tag for image
           If tag is not found in registry, than image must be rebuilt
        """
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
            'files': {path: hashlib.sha256(data.encode()).hexdigest()
                      for path, data in self.files.items()},
        }, sort_keys=True)
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
            if self.command:
                dockerfile.write('CMD {}\n'.format(self.command).encode())
            for path, data in self.files.items():
                dockerfile.write('ADD {} {}\n'.format(path, path).encode())
                tinfo = tarfile.TarInfo(path)
                tinfo.size = len(data)
                fileobj = io.BytesIO(data.encode())
                tfile.addfile(tinfo, fileobj)
            dfinfo = tarfile.TarInfo('Dockerfile')
            dfinfo.size = len(dockerfile.getvalue())
            dockerfile.seek(0)
            tfile.addfile(dfinfo, dockerfile)

        f.seek(0)
        return f

    def getports(self):
        return self.ports


class Container:
    def __init__(self, name: str, ship: Ship, image: Image, command: str=None, hostname: str=None,
                 ports: dict=None, memory: int=0, volumes: dict=None, env: dict=None, extports: dict=None,
                 portproto: dict=None, network_mode: str='', user=None):
        self.name = name
        self.ship = ship
        self.image = image
        self.command = command
        self.volumes = volumes or {}
        self.ports = ports or {}
        self.memory = memory
        self.env = env or {}
        self.extports = extports or {}
        self.portproto = portproto or {}
        self.id = ''
        self.status = 'not found'
        self.hostname = hostname or '{}-{}'.format(self.name, self.ship.name)
        self.network_mode = network_mode
        self.shipment = None
        self.user = user

    def __repr__(self):
        return 'Container(name={name}, ship={ship}, Image={image}, env={env}, id={id})'.format(**vars(self))

    def __getstate__(self):
        return {k: v for k, v in vars(self).items() if k not in ['id', 'status']}

    @property
    def logger(self):
        return utils.getlogger(container=self, bindto=3)

    def __setstate__(self, state):
        self.__dict__.update(state)
        self.id = ''
        self.status = 'not found'

    def getvolume(self, volumename):
        return self.volumes[volumename]

    @property
    def running(self):
        return 'Up' in self.status

    def getfullname(self):
        return '{}.{}'.format(self.shipment.name, self.name) if self.shipment else self.name

    def check(self, cinfo=None):
        """This function tries to find container on the associated ship
        by listing all containers. If found, it fills `id` and `status` attrs.
        If `cinfo` is provided, then skips docker api call for container listing.
        """
        if cinfo is None:
            self.logger.debug('checking container status')
            matched = [cont for cont in self.ship.docker.containers(all=True)
                       if cont['Names'] and cont['Names'][0][1:] == self.getfullname()]
            if len(matched) > 0:
                cinfo = matched[0]

        if cinfo:
            self.id = cinfo.get('Id', self.id)
            self.status = cinfo.get('Status', self.status)
        else:
            self.id = ''
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
        self.logger.bind(follow=follow).debug('getting logs from container')
        try:
            if follow:
                lines = utils.docker_lines(self.ship.docker.logs(self.id, stream=True))
            else:
                lines = self.ship.docker.logs(self.id).decode().split('\n')
            for line in lines:
                print(line)
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
        self.check({'Id': '', 'Status': 'not found'})

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
            name=self.getfullname(),
            ports=list(self.ports.values()),
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
                    '{}/{}'.format(port, self.portproto.get(name, 'tcp')): ('::', self.extports.get(name, port))
                    for name, port in self.ports.items()
                },
                binds={v.getpath(self): {'bind': v.dest, 'ro': v.ro} for v in self.volumes.values()},
                network_mode=self.network_mode,
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
        return self.extports.get(name, self.ports[name])


class Volume:
    def __repr__(self):
        return '{}(dest={dest})'.format(type(self).__name__, **vars(self))

    @property
    def logger(self):
        return utils.getlogger(volume=self, bindto=3)


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

    def getpath(self, container):
        return self.path or os.path.expanduser(os.path.join(container.ship.datadir,
                                               container.shipment.name, container.name, self.dest[1:]))


class ConfigVolume(Volume):
    def __init__(self, dest: str, files: dict=None):
        self.dest = dest
        self.files = files or {}

    def getpath(self, container):
        return os.path.expanduser(os.path.join(container.ship.configdir,
                                               container.shipment.name, container.name, self.dest[1:]))

    def getfilepath(self, filename):
        assert filename in self.files, "no such file in config volume"
        return os.path.join(self.dest, filename)

    @property
    def ro(self):
        return True

    def render(self, container):
        self.logger.debug('rendering')
        with tempfile.TemporaryDirectory() as tempdir:
            for name, file in self.files.items():
                file.dump(container, os.path.join(tempdir, name))
            container.ship.upload(tempdir, self.getpath(container))

    @utils.aslist
    def compare_files(self, container):
        self.logger.debug('comparing files')
        with tempfile.TemporaryDirectory() as tempdir:
            container.ship.download(self.getpath(container), tempdir)
            for name, file in self.files.items():
                try:
                    actual = file.load(container, os.path.join(tempdir, name))
                except FileNotFoundError:
                    actual = ''
                expected = file.data(container)
                if actual != expected:
                    diff = difflib.Differ().compare(actual.split('\n'), expected.split('\n'))
                    yield ('volumes', self.dest, 'files', name), [line for line in diff if line[:2] != '  ']


class BaseFile:
    def __str__(self):
        return '{}({!s:.20})'.format(type(self).__name__, self.content)

    @property
    def logger(self):
        return utils.getlogger(file=self, bindto=3)

    def dump(self, container: Container, path: str, data: str=None):
        if data is None:
            data = self.data(container)
        self.logger.debug("writing file", path=path)
        with open(path, 'w+', encoding='utf8') as f:
            f.write(data)

    def load(self, container: Container, path: str):
        self.logger.debug("loading text file contents", path=path)
        with open(path) as f:
            return f.read()


class TextFile(BaseFile):
    def __init__(self, filename: str=None, text: str=None):
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

    def data(self, _container):
        return self.content


class TemplateFile:
    def __init__(self, file: BaseFile, **kwargs):
        self.file = file
        self.context = kwargs

    def __str__(self):
        return 'TemplateFile(file={file}, context={context})'.format(vars(self))

    @property
    def logger(self):
        return utils.getlogger(file=self, bindto=3)

    def dump(self, container: Container, path: str):
        self.logger.debug("rendering file")
        self.file.dump(container, path, self.data(container))

    def data(self, container):
        template = mako.template.Template(self.file.data(container))
        context = {'this': container}
        context.update(self.context)
        self.logger.debug('rendering template file', context=context)
        return template.render(**context)

    def load(self, container: Container, path: str):
        return self.file.load(container, path)


class YamlFile(BaseFile):
    def __init__(self, data: dict):
        self.content = data

    def data(self, _container):
        return yaml.dump(self.content)


class JsonFile(BaseFile):
    def __init__(self, data: dict):
        self.content = data

    def data(self, _container):
        return json.dumps(self.content, sort_keys=True, indent='  ')


class Shipment:
    def __init__(self, name, containers):
        self.name = name
        self.containers = []
        for container in containers:
            container.shipment = self
            self.containers.append(container)

        self.ships = sorted(list({container.ship for container in self.containers}), key=lambda s: s.name)

        for ship in self.ships:
            ship.shipment = self

        # HACK: add "_ships" field to place it before "ships" field in yaml
        self._ships = self.ships

    @property
    def images(self):
        def compare_source_images(x, y):
            if isinstance(x, SourceImage) and isinstance(y, SourceImage):
                if x.parent is y:
                    return 1
                if x is y.parent:
                    return -1
            return 0
        return sorted(list(set(container.image for container in self.containers)),
                      key=functools.cmp_to_key(compare_source_images))

    @utils.makesorted(lambda c: (c.ship.name, c.name))
    def filter_containers(self, shipname: str=None, containername: str=None):
        notfound = True
        for cont in self.containers:
            if ((shipname is None or re.match(shipname, cont.ship.name)) and
               (containername is None or re.match(containername, cont.name))):
                notfound = False
                yield cont
        if notfound:
            utils.getlogger(shipname=shipname, containername=containername).error('no containers matched')

    def group_containers(self, shipname: str=None, containername: str=None):
        return [(ship, self.filter_containers(ship.name, containername))
                for ship in self.ships if shipname is None or re.match(shipname, ship.name)]
