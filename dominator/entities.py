import logging
import os.path
import os
import socket

from .utils import cached, ship_memory_from_nova, ship_memory_from_bot
from .settings import settings

_logger = logging.getLogger(__name__)


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


class Container:
    def __init__(self, name: str, ship: Ship, repository: str, tag: str,
                 volumes: list, ports: dict, memory: int, env: dict={}):
        self.name = name
        self.ship = ship
        self.repository = repository
        self.tag = tag
        self.volumes = volumes
        self.ports = ports
        self.memory = memory
        self.env = env

    def __repr__(self):
        return 'Container(name={name}, repository={repository}, tag={tag})'.format(**vars(self))

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
    def __init__(self, dest: str, path: str, name: str='data', ro=False):
        super(DataVolume, self).__init__(name, dest)
        self.path = path
        self.ro = ro

    def getpath(self, _container):
        return self.path


class ConfigVolume(Volume):
    def __init__(self, dest: str, files: dict={}, name: str='config'):
        super(ConfigVolume, self).__init__(name, dest)
        self.dest = dest
        self.files = files

    def getpath(self, container):
        return os.path.expanduser(os.path.join(settings['configvolumedir'],
                                               container.ship.name, container.name, self.name))

    @property
    def ro(self):
        return True


class TextFile:
    def __init__(self, filename: str, text: str=None):
        self.name = filename
        if text is not None:
            self.content = text
        else:
            with open(filename) as f:
                self.content = f.read()

    def dump(self, container, volume):
        self._write(volume.getpath(container), self.content)

    def _write(self, volumepath, data):
        path = os.path.join(volumepath, self.name)
        _logger.debug('writing file to %s', path)
        with open(path, 'w+', encoding='utf8') as f:
            f.write(data)


class TemplateFile(TextFile):
    def __init__(self, filename: str, **kwargs):
        super(TemplateFile, self).__init__(filename)
        self.context = kwargs

    def dump(self, container, volume):
        _logger.debug('rendering file %s', self.name)
        import mako.template
        template = mako.template.Template(self.content)
        context = {'this': container}
        context.update(self.context)
        _logger.debug('context is %s', context)
        self._write(volume.getpath(container), template.render(**context))
