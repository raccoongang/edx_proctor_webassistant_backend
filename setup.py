import os
from os.path import join, dirname, split
from setuptools import setup, find_packages


# with open('requirements.txt', 'r') as f:
#     requirements = f.readlines()


setup(
    name='edx_proctor_webassistant_backend',
    version='1.0',
    description='Proctoring backend',
    author='raccoongang',
    url='https://github.com/raccoongang/edx_proctor_webassistant_backend',
    
    # install_requires=requirements,
    packages=find_packages(exclude=['tests']),
)
