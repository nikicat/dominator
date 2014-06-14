import functools
import itertools
import json
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


def get_image(repo, tag='latest'):
    import docker
    dock = docker.Client()
    if tag not in get_tags(dock, repo):
        pull_repo(dock, repo, tag)
    return get_tags(dock, repo)[tag]


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
