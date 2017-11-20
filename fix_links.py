#! /usr/bin/env python
"""
Cleans up relative cross-notebook links by replacing them with .html
extension.
"""
import os
import re
from bs4 import BeautifulSoup

import holoviews as hv
import param

# TODO: holoviews specific links e.g. to reference manual...doc & generalize

# TODO: such a regex appears in at least one other place in ioam-builder...
rx = re.compile('(.*)\d\d-(.+).ipynb')
rx2 = re.compile('(.*)\d-(.+).ipynb')

#BOKEH_REPLACEMENTS = {'cell.output_area.append_execute_result': '//cell.output_area.append_execute_result',
#                      '}(window));\n</div>': '}(window));\n</script></div>',
#                      '\n(function(root) {': '<script>\n(function(root) {'}

# Fix gallery links (e.g to the element gallery)
#LINK_REPLACEMENTS = {'../../examples/elements/':'../gallery/elements/',
#                     '../../examples/demos/':'../gallery/demos/',
#                     '../../examples/streams/':'../gallery/streams/'}


def filter_available(names, name_type):
    available = []
    for name in names:
        reference_dir = os.path.abspath(os.path.join(__file__, '..','..', '..',
                                                     'examples', 'reference'))
#        if not os.path.isdir(reference_dir):
#            raise Exception('Cannot find examples/reference in %r' % reference_dir)

        for backend in ['bokeh', 'matplotlib', 'plotly']:
            candidate = os.path.join(reference_dir, name_type, backend, name+'.ipynb')
            if os.path.isfile(candidate):
                replacement_tpl = """<a href='../reference/{clstype}/{backend}/{clsname}.html'>
                <code>{clsname}</code></a>"""
                replacement = replacement_tpl.format(clstype=name_type,
                                                     clsname=name,
                                                     backend=backend)
                available.append((name, replacement))
                break
    return available


def find_autolinkable():
    # Class names for auto-linking
    excluded_names = { 'UniformNdMapping', 'NdMapping', 'MultiDimensionalMapping',
                       'Empty', 'CompositeOverlay', 'Collator', 'AdjointLayout'}
    dimensioned = set(param.concrete_descendents(hv.Dimensioned).keys())

    all_elements = set(param.concrete_descendents(hv.Element).keys())
    all_streams = set(param.concrete_descendents(hv.streams.Stream).keys())
    all_containers = set((dimensioned - all_elements) - excluded_names)
    return {'elements':   filter_available(all_elements, 'elements'),
            'streams':    filter_available(all_streams, 'streams'),
            'containers': filter_available(all_containers, 'containers')}


autolinkable = find_autolinkable()

def component_links(text, path):
    if ('user_guide' in path) or ('getting_started' in path):
        for clstype, listing in autolinkable.items():
            for (clsname, replacement) in list(listing):
                try:
                    text, count = re.subn('<code>\s*{clsname}\s*</code>*'.format(clsname=clsname),replacement, text)
                except Exception as e:
                    print(str(e))
    return text


def cleanup_links(path):
    with open(path) as f:
        text = f.read()

#    if 'BokehJS does not appear to have successfully loaded' in text:
#        for k, v in BOKEH_REPLACEMENTS.items():
#            text = text.replace(k, v)

    text = component_links(text, path)
    soup = BeautifulSoup(text)
    for a in soup.findAll('a'):
        href = a.get('href', '')
        if '.ipynb' in href and 'http' not in href:
 #           for k, v in LINK_REPLACEMENTS.items():
 #               href = href.replace(k, v)
            if rx.match(href):
                parts = href.split('/')
                a['href'] = '/'.join(parts[:-1]+[parts[-1][3:-5]+'html'])
            elif rx2.match(href):
                parts = href.split('/')
                a['href'] = '/'.join(parts[:-1]+[parts[-1][2:-5]+'html'])
            else:
                a['href'] = href.replace('.ipynb', '.html')
    html = soup.prettify("utf-8").decode('utf-8')
    with open(path, 'w') as f:
        f.write(html)

if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument('build_dir', help="Build Directory")
    args = parser.parse_args()

    for root, dirs, files in os.walk(args.build_dir):
        for file_path in files:
            if file_path.endswith(".html"):
                soup = cleanup_links(os.path.join(root, file_path))
