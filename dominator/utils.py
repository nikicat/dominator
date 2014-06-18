import functools
import itertools
import json
import difflib
import structlog

from .settings import settings

_logger = structlog.get_logger()


def cached(fun):
    return functools.lru_cache(100)(fun)


def groupby(objects, key):
    return itertools.groupby(sorted(objects, key=key), key=key)


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


@asdict
def get_tags(dock, repo):
    images = dock.images(repo, all=True)
    for image in images:
        yield image['RepoTags'][0].split(':')[-1], image['Id']


def get_docker(url=None):
    import docker
    return docker.Client(url or settings.get('dockerurl'))


def get_repo(repo):
    if '/' not in repo and 'docker-namespace' in settings:
        return '{}/{}'.format(settings['docker-namespace'], repo)
    else:
        return repo


def get_image(repo, tag='latest'):
    dock = get_docker()
    if tag not in get_tags(dock, repo):
        pull_repo(dock, repo, tag)
    return get_tags(dock, repo)[tag]


@aslist
def image_ports(imageid):
    dock = get_docker()
    for port in dock.inspect_image(imageid)['config']['ExposedPorts'].keys():
        yield int(port.split('/')[0])


def image_command(imageid):
    return ' '.join(get_docker().inspect_image(imageid)['config']['Cmd'])


@asdict
def image_env(imageid):
    return [var.split('=', 1) for var in get_docker().inspect_image(imageid)['config']['Env']]


def pull_repo(dock, repo, tag=None):
    logger = _logger.bind(repo=repo, tag=tag)
    logger.info('pulling repo')
    for line in dock.pull(repo, tag, stream=True):
        if line != '':
            logger.debug('received line', line=line)
            resp = json.loads(line)
            if 'status' in resp:
                logger.debug(resp['status'])
            elif 'error' in resp:
                raise RuntimeError('could not pull {} ({})'.format(repo, resp['error']))


@aslist
def compare_env(expected: dict, actual: dict):
    for name, value in actual.items():
        if name not in expected:
            yield name, '', value or '""'
        elif expected[name] != value:
            yield name, expected[name], value or '""'

    for name, value in expected.items():
        if name not in actual:
            yield name, value or '""', ''


@aslist
def compare_ports(cont, actual: dict):
    for name, port_expected in cont.ports.items():
        extport_expected = cont.extports.get(name, port_expected)
        proto_expected = cont.portproto.get(name, 'tcp')

        matched_actual = [info for name, info in actual.items()
                          if name == '{}/{}'.format(port_expected, proto_expected)]

        if len(matched_actual) == 0:
            yield name, [('int', port_expected, ''),
                         ('ext', extport_expected, ''),
                         ('proto', proto_expected, '')]
        else:
            extport_actual = int(matched_actual[0][0]['HostPort'])
            if extport_actual != extport_expected:
                yield name, [('ext', extport_expected, extport_actual)]

    for portname, portinfo in actual.items():
        matched_expected = [name for name, port in cont.ports.items()
                            if portname == '{}/{}'.format(port, cont.portproto.get(name, 'tcp'))]
        if len(matched_expected) == 0:
            port, proto = portname.split('/')
            yield port, [('int', '', port),
                         ('ext', '', portinfo[0]['HostPort']),
                         ('proto', '', proto)]


@aslist
def compare_volumes(cont, cinfo):
    for dest, path in cinfo['Volumes'].items():
        ro = not cinfo['VolumesRW'][dest]
        matched_expected = [volume for volume in cont.volumes if volume.dest == dest]
        if len(matched_expected) == 0:
            if not path.startswith('/var/lib/docker/vfs/dir'):
                yield dest, [('path', '', path),
                             ('ro', '', ro)]
        else:
            volume = matched_expected[0]
            diffs = []
            if volume.getpath(cont) != path:
                diffs.append(('path', volume.path, path))
            if volume.ro != ro:
                diffs.append(('ro', volume.ro, ro))
            if hasattr(volume, 'files'):
                configdiff = compare_files(cont, volume)
                if len(configdiff) > 0:
                    diffs.append(('files', configdiff))

            if len(diffs) > 0:
                yield dest, diffs

    for volume in cont.volumes:
        matched_actual_path = [path for dest, path in cinfo['Volumes'].items() if dest == volume.dest]
        if len(matched_actual_path) == 0:
            yield volume.dest, [('path', volume.getpath(cont), ''),
                                ('ro', volume.ro, '')]


@aslist
def compare_files(container, volume):
    for file in volume.files:
        actual = file.load(container, volume)
        expected = file.data(container)
        if actual != expected:
            diff = difflib.Differ().compare(actual.split('\n'), expected.split('\n'))
            yield file.name, [line for line in diff if line[:2] != '  ']


@aslist
def compare_container(cont, cinfo):
    imagerepo, imageid = cinfo['Config']['Image'].split(':')

    for key, expected, actual in [
        ('name', cont.name, cinfo['Name'][1:]),
        ('image.repo', cont.image.repository, imagerepo),
        ('image.id', cont.image.id, imageid),
        ('command', cont.command or cont.image.command, ' '.join(cinfo['Config']['Cmd'])),
        ('memory', cont.memory, cinfo['Config']['Memory']),
    ]:
        if expected != actual:
            yield key, expected, actual

    for key, subkeys in [('env', compare_env(dict(list(cont.image.env.items()) + list(cont.env.items())),
                                             dict(var.split('=', 1) for var in cinfo['Config']['Env']))),
                         ('ports', compare_ports(cont, cinfo['NetworkSettings']['Ports'])),
                         ('volumes', compare_volumes(cont, cinfo))]:
        if len(subkeys) > 0:
            yield key, subkeys


@aslist
def ships_from_conductor(name):
    import conductor_client
    from .entities import Ship
    for host in conductor_client.Group(name=name).hosts:
        yield Ship(
            fqdn=host.fqdn,
            datacenter=host.datacenter.name,
            name=host.fqdn.split('.')[0],
        )


def _nova_client(cluster):
    from novaclient.v1_1 import client
    novaconfig = settings['nova'][cluster]['client']
    _logger.debug('creating nova client', **novaconfig)
    return client.Client(**novaconfig)


@aslist
def ships_from_nova(cluster, metadata):
    from .entities import Ship
    nova = _nova_client(cluster)
    for server in nova.servers.findall():
        for k, v in metadata.items():
            if server.metadata.get(k) != v:
                break
        else:
            yield Ship(
                name=server.name,
                fqdn='{}.{}'.format(server.name, settings['nova'][cluster]['domain']),
                novacluster=cluster,
            )


def datacenter_from_racktables(hostname):
    import requests
    import pyquery
    r = requests.get(
        url=settings['racktables']['url'],
        auth=(settings['racktables']['user'], settings['racktables']['password']),
        params={'page': 'search', 'q': hostname},
    )
    return pyquery.PyQuery(r.text)('a.tag-279').text()


def ship_memory_from_bot(fqdn):
    import requests
    # BOT response format: 'ok:64GB'
    r = requests.get('http://bot.yandex-team.ru/api/ram-summary.php?name={}'.format(fqdn))
    if r.status_code != 200 or r.text[:2] != 'ok':
        raise RuntimeError('failed to get RAM for {} from BOT'.format(fqdn))
    return int(r.text.split(':')[1][:-3]) * 1024**3


def ship_memory_from_nova(ship):
    c = _nova_client(ship.novacluster)
    return c.flavors.get(c.servers.find(name=ship.name).flavor['id']).ram * 1024**2
