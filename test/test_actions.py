import tempfile
import logging
import re
import pytest
import vcr
from colorama import Fore

from dominator.entities import LocalShip, Container, Image
from dominator.actions import dump, run, status, initlog, load_yaml
from dominator.settings import settings as _settings


@pytest.yield_fixture(autouse=True)
def settings():
    with tempfile.TemporaryDirectory() as configdir:
        _settings['configvolumedir'] = configdir
        yield _settings


@pytest.fixture(autouse=True)
def logs():
    initlog()
    logging.basicConfig(level=logging.DEBUG)


@pytest.fixture
def ships():
    return [LocalShip()]


@pytest.fixture
def containers():
    return [Container(name='testcont',
                      ship=ship,
                      image=Image('busybox'),
                      command='sleep 10') for ship in ships()]


@pytest.fixture
def docker():
    import docker
    return docker.Client()


@vcr.use_cassette('fixtures/vcr_cassettes/run.yaml')
def test_run(capsys, containers):
    # FIXME: use https endpoint because vcrpy doesn't handle UnixHTTPConnection
    run(containers, dockerurl='http://localhost:4243')
    _, _ = capsys.readouterr()
    status(containers)
    out, _ = capsys.readouterr()
    assert re.match(r'  testcont[ \t]+Up Less than a second[ \t]+{color}{id:.7}'.format(
        color=re.escape(Fore.GREEN), id=containers[0].image.id), out.split('\n')[-2])


def test_dump(capsys, containers):
    dump(containers)
    dump1, _ = capsys.readouterr()
    assert dump1 != ''
    with tempfile.NamedTemporaryFile(mode='w') as tmp:
        tmp.write(dump1)
        tmp.flush()
        dump(load_yaml(tmp.name))
    dump2, _ = capsys.readouterr()
    assert dump1 == dump2
