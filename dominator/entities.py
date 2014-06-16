import os.path
import os
import socket
import inspect

import yaml
import pkg_resources
import structlog

from .utils import cached, ship_memory_from_nova, ship_memory_from_bot, get_image
from .settings import settings

_logger = structlog.get_logger()


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
    @cached
    def memory(self):
        if hasattr(self, 'novacluster'):
            return ship_memory_from_nova(self)
        else:
            return ship_memory_from_bot(self.fqdn)

    @property
    @cached
    def islocal(self):
        return self.name == os.uname()[1]

    def containers(self, containers):
        return [c for c in containers if c.ship == self]


class LocalShip(Ship):
    def __init__(self):
        super(LocalShip, self).__init__(
            name=os.uname()[1],
            fqdn=socket.gethostbyname(socket.gethostname()),
            datacenter='local',
        )

    @property
    @cached
    def memory(self):
        import psutil
        return psutil.avail_phymem()


class Image:
    def __init__(self, repository: str, tag: str='latest', id: str=None):
        self.repository = repository
        self.tag = tag
        self.id = id or self.getid()

    def __repr__(self):
        return 'Image(repository={repository}, tag={tag}, id={id:.7})'.format(**vars(self))

    def getid(self):
        return get_image(self.repository, self.tag)


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


class TextFile:
    def __init__(self, filename: str, text: str=None):
        self.name = filename
        if text is not None:
            self.content = text
        else:
            parent_frame = inspect.stack()[1]
            parent_module = inspect.getmodule(parent_frame[0])
            self.content = pkg_resources.resource_string(parent_module.__name__, filename).decode()

    def __str__(self):
        return 'TextFile(name={})'.format(self.name)

    def dump(self, container, volume):
        self._write(volume.getpath(container), self.content)

    def _write(self, volumepath, data):
        path = os.path.join(volumepath, self.name)
        _logger.debug("writing file", path=path)
        with open(path, 'w+', encoding='utf8') as f:
            f.write(data)


class TemplateFile:
    def __init__(self, file: TextFile, **kwargs):
        self.file = file
        self.context = kwargs

    def __str__(self):
        return 'TemplateFile(file={file}, context={context})'.format(vars(self))

    def dump(self, container, volume):
        logger = _logger.bind(file=self)
        logger.debug("rendering file")
        import mako.template
        template = mako.template.Template(self.file.content)
        context = {'this': container}
        context.update(self.context)
        logger.debug('context', context=context)
        self.file._write(volume.getpath(container), template.render(**context))


class YamlFile:
    def __init__(self, name: str, data: dict):
        self.name = name
        self.data = data

    def __str__(self):
        return 'YamlFile(name={name})'.format(vars(self))

    def dump(self, container, volume):
        _logger.debug("rendering file", file=self)
        with open(os.path.join(volume.getpath(container), self.name), 'w+') as f:
            yaml.dump(self.data, f)
