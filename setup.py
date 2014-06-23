import sys
import setuptools
from setuptools.command.test import test as TestCommand


class PyTest(TestCommand):
    user_options = [('pytest-args=', 'a', "Arguments to pass to py.test")]

    def initialize_options(self):
        TestCommand.initialize_options(self)
        self.pytest_args = None

    def finalize_options(self):
        TestCommand.finalize_options(self)
        self.test_args = []
        self.test_suite = True

    def run_tests(self):
        # import here, cause outside the eggs aren't loaded
        import pytest
        errno = pytest.main(self.pytest_args)
        sys.exit(errno)


if __name__ == '__main__':
    setuptools.setup(
        name='dominator',
        version='0.4',
        url='https://github.com/nikicat/dominator',
        license='GPLv3',
        author='Nikolay Bryskin',
        author_email='devel.niks@gmail.com',
        description='Cloud deployment toolbox',
        platforms='linux',
        packages=['dominator'],
        entry_points={'console_scripts': ['dominator = dominator:main']},
        package_data={'dominator': ['settings.yaml']},
        classifiers=[
            'License :: OSI Approved :: GNU General Public License v3 (GPLv3)',
            'Operating System :: POSIX :: Linux',
            'Programming Language :: Python :: 3',
            'Topic :: Software Development :: Libraries :: Python Modules',
            'Topic :: System :: Distributed Computing',
        ],
        install_requires=[
            'docker-py >= 0.4.2-dev',
            'docopt',
            'pyyaml == 3.11nikicat',
            'mako',
            'colorlog',
        ],
        dependency_links=[
            'hg+https://bitbucket.org/nikicat/pyyaml#egg=pyyaml-3.11nikicat',
            'git+https://github.com/dotcloud/docker-py#egg=docker-py-0.4.2-dev',
        ],
        install_recommends=[
            'requests',
            'python_novaclient',
            'conductor_client',
            'psutil',
            'colorama',
            'pyquery',
        ],
        tests_require=[
            'pytest',
            'vcrpy',
            'colorama',
        ],
        cmdclass={'test': PyTest},
    )
