"""
This module contains classes used to describe cluster configuration
"""


import os.path
import os
import inspect
import json
import contextlib

import yaml
import pkg_resources
import docker

from . import utils
from .settings import settings


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
    def memory(self):
        if hasattr(self, 'novacluster'):
            return utils.ship_memory_from_nova(self)
        else:
            return utils.ship_memory_from_bot(self.fqdn)

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
        return settings.get('localship-fqdn', 'localhost')

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
        return docker.Client(settings.get('dockerurl'))


class Image:
    def __init__(self, repository: str, tag: str='latest', id: str=''):
        self.tag = tag
        self.id = id
        self.repository = utils.getrepo(repository)
        if not self.id:
            self.getid()

    def __repr__(self):
        return 'Image(repository={repository}, tag={tag}, id={id:.7})'.format(**vars(self))

    @property
    def logger(self):
        return utils.getlogger(image=self, bindto=3)

    def getid(self):
        self.logger.debug('retrieving id')
        dock = utils.getdocker()
        if self.tag not in self.tags:
            self.pull(dock)
        self.id = self.tags[self.tag]

    def pull(self, dock):
        self.logger.info('pulling repo')
        logger = utils.getlogger('dominator.docker.pull', image=self, docker=dock)
        for line in dock.pull(self.repository, self.tag, stream=True):
            if line != '':
                resp = json.loads(line)
                if 'error' in resp:
                    raise RuntimeError('could not pull {} ({})'.format(self.repository, resp['error']))
                else:
                    logger.debug('', response=resp)
        Image.tags.fget.cache_clear()

    @property
    @utils.cached
    @utils.asdict
    def tags(self):
        self.logger.debug('retrieving tags')
        images = utils.getdocker().images(self.repository, all=True)
        for image in images:
            yield image['RepoTags'][0].split(':')[-1], image['Id']

    def inspect(self):
        result = utils.getdocker().inspect_image(self.id)
        # Workaround: Docker sometimes returns "config" key in different casing
        if 'config' in result:
            return result['config']
        elif 'Config' in result:
            return result['Config']
        else:
            raise RuntimeError("unexpected response from Docker: {}".format(result))

    @property
    @utils.cached
    def ports(self):
        return [int(port.split('/')[0]) for port in self.inspect()['ExposedPorts'].keys()]

    @property
    def command(self):
        return ' '.join(self.inspect()['Cmd'])

    @property
    def env(self):
        return dict(var.split('=', 1) for var in self.inspect()['Env'])


class Container:
    def __init__(self, name: str, ship: Ship, image: Image, command: str=None, hostname: str=None,
                 ports: dict={}, memory: int=0, volumes: list=[],
                 env: dict={}, extports: dict={}, portproto: dict={}):
        self.name = name
        self.ship = ship
        self.image = image
        self.command = command
        self.volumes = volumes
        self.ports = ports
        self.memory = memory
        self.env = env
        self.extports = extports
        self.portproto = portproto
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
        for volume in self.volumes:
            if volume.name == volumename:
                return volume
        else:
            raise RuntimeError('no such volume in container: %s', volumename)

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
        self.ship.docker.remove_container(self.id, force=force)
        self.check({'Id': '', 'Status': 'not found'})

    def create(self):
        self.logger.debug('preparing to create container')

        for volume in self.volumes:
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
        self.logger.debug('creating container')
        return self.ship.docker.create_container(
            image='{}:{}'.format(self.image.repository, self.image.id),
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
        try:
            self.create()
        except docker.errors.APIError as e:
            if e.response.status_code != 409:
                raise
            self.check()
            if self.id:
                if self.running:
                    self.logger.info('found running container with the same name, comparing config with requested')
                    diff = utils.compare_container(self, self.inspect())
                    if diff:
                        self.logger.info('running container config differs from requested, stopping', diff=diff)
                        self.stop()
                    else:
                        self.logger.info('running container config identical to requested, keeping')
                        return

                self.logger.info('found stopped container with the same name, removing')
                self.remove()
            self.create()

        self.start()

    def start(self):
        self.logger.debug('starting container')
        self.ship.docker.start(
            self.id,
            port_bindings={
                '{}/{}'.format(port, self.portproto.get(name, 'tcp')): ('::', self.extports.get(name, port))
                for name, port in self.ports.items()
            },
            binds={v.getpath(self): {'bind': v.dest, 'ro': v.ro} for v in self.volumes},
        )
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
        return '{}(name={name}, dest={dest})'.format(type(self).__name__, **vars(self))

    @property
    def logger(self):
        return utils.getlogger(volume=self, bindto=3)


class DataVolume(Volume):
    def __init__(self, dest: str, path: str=None, name: str='data', ro=False):
        self.name = name
        self.dest = dest
        self.path = path
        self.ro = ro

    def render(self, _):
        pass

    def getpath(self, container):
        return self.path or os.path.expanduser(os.path.join(settings['datavolumedir'],
                                                            container.name, self.name))


class ConfigVolume(Volume):
    def __init__(self, dest: str, files: dict={}, name: str='config'):
        self.name = name
        self.dest = dest
        self.files = files

    def getpath(self, container):
        return os.path.expanduser(os.path.join(settings['configvolumedir'],
                                               container.name, self.name))

    @property
    def ro(self):
        return True

    def render(self, cont):
        self.logger.debug('rendering')
        path = self.getpath(cont)
        os.makedirs(path, exist_ok=True)

        for filename in os.listdir(path):
            os.remove(os.path.join(path, filename))
        for file in self.files:
            file.dump(cont, self)


class BaseFile:
    def __init__(self, name):
        self.name = name

    @property
    def logger(self):
        return utils.getlogger(file=self, bindto=3)

    def getpath(self, container: Container, volume: Volume):
        return os.path.join(volume.getpath(container), self.name)

    def dump(self, container: Container, volume: Volume, data: str=None):
        if data is None:
            data = self.data(container)
        path = self.getpath(container, volume)
        self.logger.debug("writing file", path=path)
        with open(path, 'w+', encoding='utf8') as f:
            f.write(data)

    def load(self, container: Container, volume: Volume):
        path = self.getpath(container, volume)
        self.logger.debug("loading text file contents", path=path)
        with open(path) as f:
            return f.read()


class TextFile(BaseFile):
    def __init__(self, filename: str, text: str=None):
        BaseFile.__init__(self, filename)
        if text is not None:
            self.content = text
        else:
            parent_frame = inspect.stack()[1]
            parent_module = inspect.getmodule(parent_frame[0])
            self.content = pkg_resources.resource_string(parent_module.__name__, filename).decode()

    def __str__(self):
        return 'TextFile(name={})'.format(self.name)

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

    def dump(self, container, volume):
        self.logger.debug("rendering file")
        self.file.dump(container, volume, self.data(container))

    def data(self, container):
        import mako.template
        template = mako.template.Template(self.file.data(container))
        context = {'this': container}
        context.update(self.context)
        self.logger.debug('rendering template file', context=context)
        return template.render(**context)

    def load(self, container, volume):
        return self.file.load(container, volume)

    @property
    def name(self):
        return self.file.name


class YamlFile(BaseFile):
    def __init__(self, name: str, data: dict):
        BaseFile.__init__(self, name)
        self.content = data

    def __str__(self):
        return 'YamlFile(name={name})'.format(vars(self))

    def data(self, _container):
        return yaml.dump(self.content)


class JsonFile(BaseFile):
    def __init__(self, name: str, data: dict):
        BaseFile.__init__(self, name)
        self.content = data

    def __str__(self):
        return 'JsonFile(name={name})'.format(vars(self))

    def data(self, _container):
        return json.dumps(self.content, sort_keys=True, indent='  ')
