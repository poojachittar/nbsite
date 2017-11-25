#!/usr/bin/env python

"""Auto-generates the rst files corresponding to the Notebooks in
examples_path

nbsite_nbpagebuild.py org project /path/to/examples /path/to/doc

By default, takes title from first cell of notebook.
The_Title.ipynb -> The Title

"""

import os
import glob
import re
import sys

# if only someone had made a way to handle parameters
org = sys.argv[1]
project = sys.argv[2]
examples_path = os.path.abspath(sys.argv[3])
doc_path = os.path.abspath(sys.argv[4])
offset = 1
if len(sys.argv) > 5:
    offset = int(sys.argv[5])
space = "_"
if len(sys.argv) > 6:
    space = sys.argv[6]

print("Making rst for notebooks in %s and putting them %s"%(examples_path,doc_path))

for filename in glob.iglob(os.path.join(examples_path,"**","*.ipynb"), recursive=True):
    fromhere = filename.split(examples_path)[1].lstrip('/')
    # TODO: decide what to do about gallery later
    if fromhere.startswith('gallery'):
        continue    
    fullpath = os.path.abspath(os.path.join(doc_path,fromhere))
    dirname = os.path.dirname(fullpath)
    os.makedirs(dirname, exist_ok=True)
    # title is filename with spaces for underscores, and no digits- prefix
    title = os.path.basename(fullpath)[:-6].replace(space, ' ')
    title = re.match('(\d*-)?(?P<title>.*)',title).group('title')
    fullpathrst = os.path.join(dirname, title) + '.rst'
    fullpathrst = fullpathrst.replace(' ','_')
    with open(fullpathrst, 'w') as rst_file:
        rst_file.write(title+'\n')
        rst_file.write('_'*len(title)+'\n\n')
        rst_file.write(".. notebook:: %s %s\n" % (project, os.path.relpath(examples_path,start=dirname)+'/'+fromhere))
        rst_file.write("    :offset: %s\n" % offset)
        rst_file.write('\n\n-------\n\n')
        # TODO: hardcoded
        rst_file.write('`Right click to download this notebook from GitHub.'
                       ' <https://raw.githubusercontent.com/%s/%s/master/examples/%s>`_\n' % (org, project,fromhere))



