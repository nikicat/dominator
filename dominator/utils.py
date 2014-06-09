import functools
import itertools
from .settings import settings


def cached(fun):
    return functools.lru_cache(100)(fun)


def groupby(objects, key):
    return itertools.groupby(sorted(objects, key=key), key=key)


def aslist(fun):
    @functools.wraps(fun)
    def wrapper(*args, **kwargs):
        return list(fun(*args, **kwargs))

    return wrapper


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
