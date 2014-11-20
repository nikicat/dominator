import setuptools

if __name__ == '__main__':
    setuptools.setup(
        name='dominator',
        version='15.0.0',
        url='https://github.com/yandex-sysmon/dominator',
        license='LGPLv3',
        author='Nikolay Bryskin',
        author_email='devel.niks@gmail.com',
        description='Cloud deployment toolbox',
        platforms='linux',
        packages=['dominator.entities', 'dominator.actions', 'dominator.utils'],
        namespace_packages=['dominator'],
        entry_points={'console_scripts': ['dominator = dominator.actions:cli']},
        package_data={
            'dominator.utils': ['*.yaml'],
            'dominator.actions': ['debian/*', 'debian/source/*'],
            'dominator.entities': ['localship.pem'],
        },
        exclude_package_data={'dominator.actions': ['debian/source']},
        classifiers=[
            'License :: OSI Approved :: GNU General Public License v3 (GPLv3)',
            'Operating System :: POSIX :: Linux',
            'Programming Language :: Python :: 3',
            'Topic :: Software Development :: Libraries :: Python Modules',
            'Topic :: System :: Distributed Computing',
        ],
        install_requires=[
            'docker-py >= 0.5.0',
            'mako',
            'colorama',
            'click',
            'mergedict',
            'tabloid',
        ],
        extras_require={
            'full': ['PyYAML.Yandex >= 3.11.1', 'colorlog', 'pkginfo', 'openssh_wrapper',
                     'objgraph', 'psutil', 'vcrpy', 'requests>=2.4', 'urllib3>=1.9.1'],
            'tiny': ['PyYAML'],
        },
    )
