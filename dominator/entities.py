import os.path
import os
import inspect

import yaml
import pkg_resources
from structlog import get_logger

from . import utils
from .settings import settings


class Ship:
    def __init__(self, name, fqdn, **kwargs):
        self.name = name
        self.fqdn = fqdn
        for k, v in kwargs.items():
            setattr(self, k, v)

    def __lt__(self, other):
        return self.fqdn < other.fqdn

    def __repr__(self):
        return 'Ship(name={})'.format(self.name)

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

    def containers(self, containers):
        return [c for c in containers if c.ship == self]


class LocalShip(Ship):
    def __init__(self):
        super(LocalShip, self).__init__(
            name=os.uname()[1],
            fqdn='localhost',
            datacenter='local',
        )

    @property
    @utils.cached
    def memory(self):
        import psutil
        return psutil.avail_phymem()


class Image:
    def __init__(self, repository: str, tag: str='latest', id: str=None):
        self.repository = utils.get_repo(repository)
        self.tag = tag
        self.id = id or self.getid()

    def __repr__(self):
        return 'Image(repository={repository}, tag={tag}, id={id:.7})'.format(**vars(self))

    def getid(self):
        return utils.get_image(self.repository, self.tag)

    @property
    def ports(self):
        return utils.image_ports(self.id)

    @property
    def command(self):
        return utils.image_command(self.id)

    @property
    def env(self):
        return utils.image_env(self.id)


class Container:
    def __init__(self, name: str, ship: Ship, image: Image, command: str=None,
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

    def __repr__(self):
        return 'Container(name={name}, ship={ship}, Image={image}, env={env})'.format(**vars(self))

    def getvolume(self, volumename):
        for volume in self.volumes:
            if volume.name == volumename:
                return volume
        else:
            raise RuntimeError('no such volume in container: %s', volumename)


class Volume:
    def __init__(self, name: str, dest: str):
        self.name = name
        self.dest = dest

    def __repr__(self):
        return '{}(name={name}, dest={dest})'.format(type(self).__name__, **vars(self))


class DataVolume(Volume):
    def __init__(self, dest: str, path: str=None, name: str='data', ro=False):
        super(DataVolume, self).__init__(name, dest)
        self.path = path
        self.ro = ro

    def getpath(self, container):
        return self.path or os.path.expanduser(os.path.join(settings['datavolumedir'],
                                                            container.name, self.name))


class ConfigVolume(Volume):
    def __init__(self, dest: str, files: dict={}, name: str='config'):
        super(ConfigVolume, self).__init__(name, dest)
        self.dest = dest
        self.files = files

    def getpath(self, container):
        return os.path.expanduser(os.path.join(settings['configvolumedir'],
                                               container.name, self.name))

    @property
    def ro(self):
        return True


class BaseFile:
    def __init__(self, name):
        self.name = name

    def getpath(self, container, volume):
        return os.path.join(volume.getpath(container), self.name)

    def dump(self, container, volume, data):
        if data is None:
            data = self.data(container)
        path = self.getpath(container, volume)
        get_logger().debug("writing file", file=self, path=path)
        with open(path, 'w+', encoding='utf8') as f:
            f.write(data)

    def load(self, container, volume):
        path = self.getpath(container, volume)
        get_logger().debug('loading text file contents', path=path)
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

    def dump(self, container, volume):
        logger = get_logger(file=self)
        logger.debug("rendering file")
        self.file.dump(container, volume, self.data(container))

    def data(self, container):
        import mako.template
        template = mako.template.Template(self.file.data(container))
        context = {'this': container}
        context.update(self.context)
        get_logger().debug('rendering template file', context=context)
        return template.render(**context)

    def load(self, container, volume):
        return self.file.load(container, volume)


class YamlFile(BaseFile):
    def __init__(self, name: str, data: dict):
        BaseFile.__init__(self, name)
        self.content = data

    def __str__(self):
        return 'YamlFile(name={name})'.format(vars(self))

    def data(self, _container):
        return yaml.dump(self.content)
