#!/usr/bin/env python
# -*- coding: latin-1 -*-
# #
# Copyright 2009-2016 Ghent University
#
# This file is part of vsc-filesystems,
# originally created by the HPC team of Ghent University (http://ugent.be/hpc/en),
# with support of Ghent University (http://ugent.be/hpc),
# the Flemish Supercomputer Centre (VSC) (https://vscentrum.be/nl/en),
# the Hercules foundation (http://www.herculesstichting.be/in_English)
# and the Department of Economy, Science and Innovation (EWI) (http://www.ewi-vlaanderen.be/en).
#
# All rights reserved.
#
# #
"""
vsc-filesystems base distribution setup.py

@author: Stijn De Weirdt (Ghent University)
@author: Andy Georges (Ghent University)
"""
import vsc.install.shared_setup as shared_setup
from vsc.install.shared_setup import ag, kh, sdw, kw, wdp


PACKAGE = {
    'version': '0.38.6',
    'author': [sdw, ag, kh],
    'maintainer': [sdw, ag, kh, kw, wdp],
    'tests_require': ['mock'],
    'install_requires': [
        'vsc-base >= 2.4.16',
        'vsc-config >= 2.0.2',
        'vsc-utils >= 1.4.6',
        'future >= 0.16.0',
    ],
}

if __name__ == '__main__':
    shared_setup.action_target(PACKAGE)
