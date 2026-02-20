# -*- coding: utf-8 -*-
from setuptools import find_packages
from setuptools import setup

setup(
    name="pioreactor-precision-temperature-plugin",
    version="0.1.1",
    license_files=("LICENSE.txt",),
    description="Precision temperature automation for Pioreactor using FIR + MLX90632",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    author="Cam Davidson-Pilon",
    author_email="info@pioreactor.com",
    url="https://github.com/Pioreactor/pioreactor-precision-temperature-plugin",
    packages=find_packages(),
    include_package_data=True,
    install_requires=["pioreactor>=25.2.0"],
    entry_points={
        "pioreactor.plugins": "pioreactor_precision_temperature_plugin = pioreactor_precision_temperature_plugin"
    },
)
