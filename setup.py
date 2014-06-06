import setuptools

if __name__ == '__main__':
    setuptools.setup(
        name='dominator',
        version='0.1',
        url='https://github.com/nikicat/dominator',
        license='GPLv3',
        author='Nikolay Bryskin',
        author_email='devel.niks@gmail.com',
        description='Cloud deployment toolbox',
        platforms='linux',
        packages=['dominator'],
        entry_points={'console_scripts': ['dominator = dominator:main']},
        classifiers=[
            'License :: OSI Approved :: GNU General Public License v3 (GPLv3)',
            'Operating System :: POSIX :: Linux',
            'Programming Language :: Python :: 3',
            'Topic :: Software Development :: Libraries :: Python Modules',
            'Topic :: System :: Distributed Computing',
        ],
        install_requires=[
            'docker-py',
            'argh',
            'pyyaml',
            'mako',
        ],
        install_recommends=[
            'requests',
            'python_novaclient',
            'conductor_client',
            'psutil',
        ],
    )
