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

import yaml
import pkg_resources
import docker
import docker.errors

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
    def __init__(self, name, fqdn, **kwargs):
        self.name = name
        self.fqdn = fqdn
        for k, v in kwargs.items():
            setattr(self, k, v)

    @property
    @utils.cached
    def islocal(self):
        return self.name == os.uname()[1]

    @property
    @utils.cached
    def docker(self):
        self.logger.debug('connecting to ship', fqdn=self.fqdn)
        return docker.Client('http://{}:4243/'.format(self.fqdn))


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


class Image:
    def __init__(self, repository: str, tag: str='latest', id: str=''):
        self.tag = tag
        self.id = id
        self.registry, self.repository = utils.getrepo(repository)

    def __repr__(self):
        return 'Image(repository={repository}, tag={tag}, id={id:.7}, registry={registry})'.format(**vars(self))

    def __getstate__(self):
        if self.id is '':
            self.getid()
        return vars(self)

    def getfullrepository(self):
        return self.repository if self.registry is None else '{}/{}'.format(self.registry, self.repository)

    @property
    def logger(self):
        return utils.getlogger(image=self, bindto=3)

    def getid(self):
        self.logger.debug('retrieving id')
        if self.id is '':
            if self.tag not in self.gettags():
                self.pull()
            self.id = self.gettags()[self.tag]
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
        Image.gettags.cache_clear()

    def push(self, dock=None):
        self.logger.info("pushing repo")
        dock = dock or utils.getdocker()
        return self._streamoperation(dock.push, repository=self.getfullrepository())

    def pull(self, dock=None):
        self.logger.info("pulling repo")
        dock = dock or utils.getdocker()
        return self._streamoperation(dock.pull, repository=self.getfullrepository(), tag=self.tag)

    def build(self, dock=None, **kwargs):
        self.logger.info("building image")
        dock = dock or utils.getdocker()
        return self._streamoperation(dock.build, tag='{}:{}'.format(self.getfullrepository(), self.tag), **kwargs)

    @utils.cached
    @utils.asdict
    def gettags(self):
        self.logger.debug("retrieving tags")
        images = utils.getdocker().images(self.getfullrepository(), all=True)
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

        def getfileobj(fileobj_or_filename):
            if isinstance(fileobj_or_filename, str):
                return pkg_resources.resource_stream(utils.getcallingmodule(3).__name__, fileobj_or_filename)
            else:
                return fileobj_or_filename

        self.files = {path: getfileobj(fileobj_or_filename) for path, fileobj_or_filename in (files or {}).items()}
        self.env = env or {}
        Image.__init__(self, name)
        self.tag = self.gethash()

    def __getstate__(self):
        """Used when dumping to yaml"""
        def filtered_state():
            return {k: v for k, v in Image.__getstate__(self).items() if k not in ['files']}
        try:
            return filtered_state()
        except docker.errors.DockerException:
            # DockerException means that needed image not found in repository and needs rebuilding
            pass
        self.build(fileobj=self.gettarfile(), custom_context=True)
        self.push()
        return filtered_state()

    def getid(self):
        try:
            return Image.getid(self)
        except docker.errors.DockerException as e:
            self.logger.info("pull failed, rebuilding (%s)", e)
            self.build()
            return Image.getid(self)

    def build(self, push=True, **kwargs):
        Image.build(self, fileobj=self.gettarfile(), custom_context=True, **kwargs)
        if push:
            self.push()

    def gethash(self):
        """Used to calculate unique identifying tag for image
           If tag is not found in registry, than image must be rebuilt
        """
        dump = json.dumps({
            'repository': self.repository,
            'parent': self.parent.gethash(),
            'scripts': self.scripts,
            'command': self.command,
            'workdir': self.workdir,
            'env': self.env,
            'volumes': self.volumes,
            'ports': self.ports,
            'files': {path: hashlib.sha256(file.seek(0) or file.read()).hexdigest()
                      for path, file in self.files.items()},
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
            for path, fileobj in self.files.items():
                dockerfile.write('ADD {} {}\n'.format(path, path).encode())
                if isinstance(fileobj, io.BytesIO):
                    tinfo = tarfile.TarInfo(path)
                    tinfo.size = len(fileobj.getvalue())
                else:
                    tinfo = tfile.gettarinfo(fileobj=fileobj, arcname=path)
                fileobj.seek(0)
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
                 ports: dict=None, memory: int=0, volumes: dict=None,
                 env: dict=None, extports: dict=None, portproto: dict=None):
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

    def check(self, cinfo=None):
        if cinfo is None:
            self.logger.debug('checking container status')
            matched = [cont for cont in self.ship.docker.containers(all=True)
                       if cont['Names'] and cont['Names'][0][1:] == self.name]
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
                self.logger.debug('', exc_info=True)
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
            name=self.name,
            ports=list(self.ports.values()),
            stdin_open=True,
            detach=False,
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
    def __init__(self, dest: str, path: str=None, ro=False):
        self.dest = dest
        self.path = path
        self.ro = ro

    def render(self, _):
        pass

    def getpath(self, container):
        return self.path or os.path.expanduser(os.path.join(utils.settings['datavolumedir'],
                                               container.name, self.dest[1:]))


class ConfigVolume(Volume):
    def __init__(self, dest: str, files: dict=None):
        self.dest = dest
        self.files = files or {}

    def getpath(self, container):
        return os.path.expanduser(os.path.join(utils.settings['configvolumedir'],
                                               container.name, self.dest[1:]))

    @property
    def ro(self):
        return True

    def render(self, cont):
        self.logger.debug('rendering')
        path = self.getpath(cont)
        os.makedirs(path, exist_ok=True)

        for filename in os.listdir(path):
            os.remove(os.path.join(path, filename))
        for name, file in self.files.items():
            file.dump(cont, self, name)


class BaseFile:
    def __str__(self):
        return '{}({!s:.20})'.format(type(self).__name__, self.content)

    @property
    def logger(self):
        return utils.getlogger(file=self, bindto=3)

    def getpath(self, container: Container, volume: Volume, name: str):
        return os.path.join(volume.getpath(container), name)

    def dump(self, container: Container, volume: Volume, name: str, data: str=None):
        if data is None:
            data = self.data(container)
        path = self.getpath(container, volume, name)
        self.logger.debug("writing file", path=path)
        with open(path, 'w+', encoding='utf8') as f:
            f.write(data)

    def load(self, container: Container, volume: Volume, name: str):
        path = self.getpath(container, volume, name)
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

    def dump(self, container, volume, name):
        self.logger.debug("rendering file")
        self.file.dump(container, volume, name, self.data(container))

    def data(self, container):
        import mako.template
        template = mako.template.Template(self.file.data(container))
        context = {'this': container}
        context.update(self.context)
        self.logger.debug('rendering template file', context=context)
        return template.render(**context)

    def load(self, container, volume, name):
        return self.file.load(container, volume, name)


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
