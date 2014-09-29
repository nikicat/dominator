import functools
import itertools
import inspect
import string
import pprint
import logging
import os.path
import glob

import pkg_resources
import yaml
import docker
import mergedict

try:
    import colorlog
    BaseFormatter = colorlog.ColoredFormatter
except ImportError:
    BaseFormatter = logging.Formatter

# import PtyInterceptor to make it accessible from utils package
from .pty import PtyInterceptor
PtyInterceptor  # to avoid flake8 warning


class PartialFormatter(string.Formatter):
    def __init__(self):
        self._retrieved = []

    def get_field(self, field_name, args, kwargs):
        try:
            val = super().get_field(field_name, args, kwargs)
        except (KeyError, AttributeError):
            val = '', field_name
        kwargs['_extra'].pop(field_name.split('.')[0], None)
        return val


class PartialLoggingFormatter(BaseFormatter):
    def formatMessage(self, record):
        return PartialFormatter().format(self._style._fmt, **vars(record))

    def formatException(self, exc_info):
        max_vars_lines = 30
        max_line_length = 100
        # First get the original formatted exception.
        exc_text = super().formatException(exc_info)
        # Now we're going to format and add the locals information.
        output_lines = [exc_text, '\n']
        tb = exc_info[2]  # This is the outermost frame of the traceback.
        while tb.tb_next:
            tb = tb.tb_next  # Zoom to the innermost frame.
        output_lines.append('Locals at innermost frame:\n')
        locals_text = pprint.pformat(tb.tb_frame.f_locals, indent=2)
        locals_lines = locals_text.split('\n')
        if len(locals_lines) > max_vars_lines:
            locals_lines = locals_lines[:max_vars_lines]
            locals_lines[-1] = '...'
        output_lines.extend(
            line[:max_line_length - 3] + '...' if len(line) > max_line_length else line
            for line in locals_lines)
        output_lines.append('\n')
        return '\n'.join(output_lines)


def getlogger(name='dominator', bindto=1, **kwargs):
    for frame, _, _, _, _, _ in inspect.stack()[1:]:
        if '__logger_context' in frame.f_locals:
            context = frame.f_locals['__logger_context'].copy()
            break
    else:
        context = {}

    context.update(kwargs)
    inspect.stack()[bindto][0].f_locals['__logger_context'] = context

    return BoundLogger(logging.getLogger(name), context=context)


class BoundLogger(logging.Logger):
    def __init__(self, logger, context):
        self.context = context
        self.logger = logger
        self.level = logger.level
        self.parent = logger.parent

    def bind(self, **kwargs):
        context = self.context.copy()
        context.update(kwargs)
        return BoundLogger(self.logger, context)

    def _log(self, level, msg, args, exc_info=None, stack_info=False, **kwargs):
        extra = kwargs.copy()
        extra.update(self.context)

        class PrettyDict(dict):
            def __format__(self, _):
                return ' '.join('{}={}'.format(k, v) for k, v in self.items())
        extra['_extra'] = PrettyDict(extra)

        self.logger._log(level, msg, args, exc_info, extra)


def cached(fun):
    return functools.lru_cache(100)(fun)


def groupbysorted(objects, key):
    return itertools.groupby(sorted(objects, key=key), key=key)


def makesorted(keyfunc):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            return sorted(func(*args, **kwargs), key=keyfunc)
        return wrapper
    return decorator


def _as(agg):
    aggfun = agg

    def decorator(fun):
        @functools.wraps(fun)
        def wrapper(*args, **kwargs):
            return aggfun(fun(*args, **kwargs))
        return wrapper
    return decorator

asdict = _as(dict)
aslist = _as(list)


@cached
def getdocker(url=None):
    url = url or settings.get('docker.url', default=None)
    getlogger(url=url).debug('creating docker client')
    return docker.Client(url)


@aslist
def compare_env(expected: dict, actual: dict):
    getlogger().debug('comparing environment')
    for name, value in actual.items():
        if name not in expected:
            yield ('env', name), ('', value or '""')
        elif str(expected[name]) != value:
            yield ('env', name), (expected[name], value or '""')

    for name, value in expected.items():
        if name not in actual:
            yield ('env', name), (str(value) or '""', '')


@aslist
def compare_ports(cont, actual: dict):
    getlogger().debug('comparing ports')
    for name, door in cont.doors.items():
        extport_expected = door.externalport

        matched_actual = [info for name, info in actual.items()
                          if name == door.portspec]

        if len(matched_actual) == 0:
            yield ('ports',), (name, '')
        else:
            yield from compare_values(('ports', name, 'ext'), extport_expected, int(matched_actual[0][0]['HostPort']))

    for portname, portinfo in actual.items():
        matched_expected = [name for name, door in cont.doors.items()
                            if portname == door.portspec]
        if len(matched_expected) == 0:
            port, proto = portname.split('/', 1)
            yield ('ports',), ('', port)


@aslist
def compare_volumes(cont, cinfo):
    getlogger().debug('comparing volumes')
    for dest, path in cinfo['Volumes'].items():
        ro = not cinfo['VolumesRW'][dest]
        matched_expected = [volume for volume in cont.volumes.values() if volume.dest == dest]
        if len(matched_expected) == 0:
            if not path.startswith('/var/lib/docker/vfs/dir'):
                yield ('volumes',), ('', dest)
        else:
            volume = matched_expected[0]
            getlogger(volume=volume).debug('comparing volume')

            if volume.fullpath != path:
                yield ('volumes', dest, 'path'), (volume.fullpath, path)
            elif hasattr(volume, 'compare_files'):
                yield from volume.compare_files()

            if volume.ro != ro:
                yield ('volumes', dest, 'ro'), (volume.ro, ro)

    for volume in cont.volumes.values():
        matched_actual_path = [path for dest, path in cinfo['Volumes'].items() if dest == volume.dest]
        if len(matched_actual_path) == 0:
            yield ('volumes',), (volume.dest, '')


def compare_values(key, expected, actual):
    if expected != actual:
        yield key, (expected, actual)


@aslist
def compare_container(cont, cinfo):
    getlogger().debug('comparing container')
    imageinfo = cinfo['Config']['Image'].split(':')
    imageid = imageinfo[-1]
    imagerepo = ':'.join(imageinfo[:-1])

    for key, expected, actual in [
        ('name', cont.dockername, cinfo['Name'][1:]),
        ('image.repo', cont.image.getfullrepository(), imagerepo),
        ('image.id', cont.image.getid(), imageid),
        ('memory', cont.memory, cinfo['Config']['Memory']),
        ('network_mode', cont.network_mode, cinfo['HostConfig']['NetworkMode']),
        ('user', cont.user or getattr(cont.image, 'user', ''), cinfo['Config']['User']),
        ('privileged', cont.privileged, cinfo['HostConfig']['Privileged']),
    ]:
        yield from compare_values((key,), expected, actual)

    if cont.image.id == imageid:
        # get command and env from image only if images are same because expected image could not even exist
        yield from compare_values(
            ('command',),
            cont.command or cont.image.getcommand(),
            ' '.join(cinfo['Config']['Cmd']))
        env = cont.image.getenv().copy()
        env.update(cont.env)
        yield from compare_env(env, dict(var.split('=', 1) for var in cinfo['Config']['Env']))

    yield from compare_ports(cont, cinfo['HostConfig']['PortBindings'] or {})
    yield from compare_volumes(cont, cinfo)


def docker_lines(records):
    buf = ''
    for record in records:
        buf += record.decode(errors='ignore')
        while '\n' in buf:
            line, buf = buf.split('\n', 1)
            yield line


def getcallingmodule(deep):
    parent_frame = inspect.stack()[1+deep]
    return inspect.getmodule(parent_frame[0])


def resource_string(name):
    return pkg_resources.resource_string(getcallingmodule(1).__name__, name).decode()


def stoppable(cmd):
    return 'trap exit TERM; {} & wait'.format(cmd)


def make_shipment(name):
    """
        Returns decorator that converts function return value from
        Container iterable to Shipment with provided name
    """
    from ..entities import Shipment

    def decorator(func):
        @functools.wraps(func)
        def wrapper():
            return Shipment(name=name, containers=func())
        return wrapper
    return decorator


NONEXISTENT_KEY = object()


class Settings:
    def __init__(self):
        self._dict = mergedict.ConfigDict()
        self.dirpath = os.path.expanduser('~/.config/dominator')

    def load(self, file):
        if file is None:
            for filename in itertools.chain(
                glob.glob('/etc/dominator/*.yaml'),
                glob.glob(os.path.join(self.dirpath, '*.yaml')),
            ):
                getlogger().debug("checking existense of %s", filename)
                if os.path.exists(filename):
                    getlogger().info("loading settings from %s", filename)
                    data = yaml.load(open(filename))
                    self._dict.merge(data)
        else:
            data = yaml.load(file)
            self._dict.merge(data)

    def get(self, path, default=NONEXISTENT_KEY, type_=None, help=None):
        logger = getlogger(key=path)
        if type_ is None and default is not NONEXISTENT_KEY and default is not None:
            type_ = type(default)
        parts = path.split('.')
        try:
            value = self._dict
            for part in parts:
                if part == '':
                    continue
                value = value[part]
            if type_ is not None:
                try:
                    value = type_(value)
                except:
                    logger.warning("could not convert config value to required type", value=value, type=type_)
                    raise
            return value
        except KeyError:
            if default is NONEXISTENT_KEY:
                logger.error("key is not found in config and no default value provided")
                raise
            else:
                logger.debug("key is not found in config, using default value", default=default)
                return default

    def __getitem__(self, path):
        return self.get(path)

    def set(self, path, value):
        getlogger().debug("overriding value for key", key=path, value=value)
        parts = path.split('.')
        section = self._dict
        for part in parts[:-1]:
            if part not in section:
                section[part] = {}
            section = section[part]
        section[parts[-1]] = value

    def __setitem__(self, path, value):
        return self.set(path, value)


settings = Settings()
