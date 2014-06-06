import logging
import os.path
import os
import socket

from .utils import cached, get_memory_from_nova, get_memory_from_bot

_logger = logging.getLogger(__name__)


class Ship:
    def __init__(self, name, fqdn, datacenter):
        self.name = name
        self.fqdn = fqdn
        self.datacenter = datacenter

    def __lt__(self, other):
        return self.fqdn < other.fqdn

    @property
    @cached
    def memory(self):
        if self.fqdn.endswith('.i.fog.yandex.net') or self.fqdn.endswith('.haze.yandex.net'):
            return get_memory_from_nova(self.name)
        else:
            return get_memory_from_bot(self.fqdn)

    @property
    @cached
    def islocal(self):
        return self.name == os.uname()[1]


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

    def makedir(self, path):
        os.makedirs(path, exist_ok=True)


class DataVolume(Volume):
    def __init__(self, dest: str, path: str, name: str='data', ro=False):
        super(DataVolume, self).__init__(name, dest)
        self.path = path
        self.ro = ro

    def prepare(self, _container):
        self.makedir(self.path)

    def getpath(self, _container):
        return self.path


class ConfigVolume(Volume):
    def __init__(self, dest: str, files: dict={}, name: str='config'):
        super(ConfigVolume, self).__init__(name, dest)
        self.dest = dest
        self.files = files

    def getpath(self, container):
        return os.path.expanduser(os.path.join('~', 'dominator', container.ship.name, container.name, self.name))

    def prepare(self, container):
        path = self.getpath(container)
        self.makedir(path)

        for filename in os.listdir(path):
            os.remove(os.path.join(path, filename))
        for file in self.files:
            file.dump(container, self)

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
        with open(os.path.join(volumepath, self.name), 'w+') as f:
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
