import re
import os
import glob
import json
import hashlib
import operator
import subprocess

import tabulate
import ifcopenshell
import pydot
import pysolr
import markdown
from bs4 import BeautifulSoup

from flask import Flask, send_file, render_template, abort, url_for, request, send_from_directory

app = Flask(__name__)

base = '/IFC/RELEASE/IFC4x3/HTML'

def make_url(fragment): return base + '/' + fragment

entity_to_package = json.load(open("entity_to_package.json", encoding="utf-8"))

navigation_entries = [
    ("Cover", "Contents", "Foreword", "Introduction"),
    ("Scope", "Normative references", "Terms, definitions, and abbreviated terms", "Fundamental concepts and assumptions"),
    ("Core data schemas", "Shared element data schemas", "Domain specific data schemas", "Resource definition data schemas"),
    ("Computer interpretable listings", "Alphabetical listings", "Inheritance listings", "Diagrams"),
    ("Examples", "Change logs", "Bibliography", "Index")
]

def to_dict(x):
    if isinstance(x, (list, tuple)):
        return type(x)(map(to_dict, x))
    else:
        return {"title": x}

def make_entries(x):
    md_root = "data/docs/schemas"
    categories = [d for d in os.listdir(md_root) if os.path.isdir(os.path.join(md_root, d))]
        
    if isinstance(x, (list, tuple)):
        return type(x)(map(make_entries, x))
    
    elif x['title'] == 'Alphabetical listings':
        url = make_url('listing')
    elif type(x['number']) == int and x['number'] >= 5:
        url = make_url('chapter-%d/' % x['number'])
    elif x['number'] in {'A', 'C'}:
        url = make_url('annex-%s.html' % x['number'].lower())
    else:
        url = '#'
    
    return dict(**x, url=url)

def make_counter(start=0):
    n = start
    def counter():
        nonlocal n
        n += 1
        if n > 14:
            return None
        if n > 8:
            return chr(ord('A') + n - 9)
        elif n >= 1:
            return n
    return counter
    
section_counter = make_counter(-4)
        
def number_entries(x):
    if isinstance(x, (list, tuple)) and set(map(type, x)) == {dict}:
        return type(x)(dict(**di, number=section_counter()) for i, di in enumerate(x))
    else:
        return type(x)(map(number_entries, x))
        
navigation_entries = make_entries(number_entries(to_dict(navigation_entries)))

def chapter_lookup(number=None, cat=None):

    def do_chapter_lookup(x):
        if isinstance(x, (list, tuple)):
            return next((v for v in map(do_chapter_lookup, x) if v is not None), None)
        if number is not None and x['number'] == number:
            return x
        if cat is not None and x['title'].split(" ")[0].lower() == cat:
            return x
            
    return do_chapter_lookup(navigation_entries)

hierarchy = json.load(open("hierarchy.json"))

entity_names = sorted(sum([schema.get('Entities', []) for _, cat in hierarchy for __, schema in cat], []))
type_names = sorted(sum([schema.get('Types', []) for _, cat in hierarchy for __, schema in cat], []))

name_to_number = {}

for i, (cat, schemas) in enumerate(hierarchy, start=5):
    for j, (schema_name, members) in enumerate(schemas, start=1):
        for k, ke in enumerate(["Types", "Entities"], start=2):
            for l, name in enumerate(members.get(ke, ()), start=1):
                name_to_number[name] = ".".join(map(str, (i,j,k,l)))

S = ifcopenshell.ifcopenshell_wrapper.schema_by_name('IFC4X3_RC1')

def generate_inheritance_graph(current_entity):
    i = S.declaration_by_name(current_entity)
    g = pydot.Graph('dot_inheritance', graph_type='graph')
    di = {
        'rankdir': 'BT',
        'ranksep': 0.2
    }
    for kv in di.items():
        g.set(*kv)

    previous = None
    while i:
        n = pydot.Node(i.name())
        di = {
            'color':'black',
            'fillcolor':'grey43',
            'fontcolor':'white',
            'height':'0.3',
            'shape':'rectangle',
            'style':'filled',
            'width':'4'
        }
        for kv in di.items():
            n.set(*kv)
        g.add_node(n)
    
        if previous:
            g.add_edge(pydot.Edge(previous, n))
            
        previous = n
        
        i = i.supertype()
        
    return g.to_string()
    

def get_node_colour(n):
    try:
        i = S.declaration_by_name(n)
    except:
        return 'gray'
    
    def is_relationship(n):
        while n:
            if n.name() == 'IfcRelationship':
                return True
            n = n.supertype()
    
    return 'yellow' if is_relationship(i) else 'dodgerblue'


def transform_graph(current_entity, graph_data, only_urls=False):
    graphs = pydot.graph_from_dot_data(graph_data)
    graph = graphs[0]
    
    all_nodes = []
    if len(graph.get_subgraphs()):
        for subgraph in graph.get_subgraphs():
            for node in subgraph.get_nodes():
                all_nodes.append(node)
    elif len(graph.get_nodes()):
        for node in graph.get_nodes():
            all_nodes.append(node)
            
    for n in all_nodes:
        if not only_urls:
            n.set('fillcolor', get_node_colour(n.get_name()))
            if n.get_name() == current_entity:
                n.set('color', 'red')
            n.set('shape', 'box')
            n.set('style', 'filled')
        n.set('URL',  url_for('resource', resource=n.get_name(), _external=True))
        
    return graph.to_string()


def process_graphviz(current_entity, md):
    def is_figure(s):
        if 'dot_figure' in s:
            return 1
        elif 'dot_inheritance' in s:
            return 2
        else:
            return 0
        
    graphviz_code = filter(is_figure, re.findall('```(.*?)```', md, re.S))
    
    for c in graphviz_code:
        hash = hashlib.sha256(c.encode('utf-8')).hexdigest()
        fn = os.path.join('svgs', current_entity + "_" + hash+'.dot')
        c2 = transform_graph(current_entity, c, only_urls=is_figure(c) == 2)
        with open(fn, "w") as f:
            f.write(c2)
        md = md.replace("```%s```" % c, '![](/svgs/%s_%s.svg)' % (current_entity, hash))
        subprocess.call(["dot", "-O", "-Tsvg", fn])
    
    return md    
    
"""
@app.route('/svgs/<entity>/<hash>.svg')
def get_svg(entity, hash):
    return send_from_directory('svgs', entity + "_" + hash + '.dot.svg');
"""

@app.route(make_url('figures/<fig>'))
def get_figure(fig):
    return send_from_directory('data/docs/figures', fig)

@app.route(make_url('lexical/<resource>.htm'))
def resource(resource):
    try:
        idx = name_to_number[resource]
    except:
        abort(404)
    
    """
    package = entity_to_package.get(resource)
    if not package:
        abort(404)
    """
        
    md = None    
    md_root = "data/docs/schemas"
    # for category in os.listdir(md_root):
    #     for module in os.listdir(os.path.join(md_root, category)):
    #         if module == package:
    
    md = os.path.join("data/docs/schemas", "*", "*", "*", resource + ".md")
    
    html = ''
                
    if glob.glob(md):
        
        md = glob.glob(md)[0]
        
        with open(md, 'r', encoding='utf-8') as f:
    
            mdc = f.read()
        
            if "Entities" in md:
    
                mdc += '\n\nEntity inheritance\n--------\n\n```' + generate_inheritance_graph(resource) + '```'
    
            html = markdown.markdown(
                process_graphviz(resource, mdc),
                extensions=['tables', 'fenced_code'])
        
            soup = BeautifulSoup(html)
        
            # First h1 is handled by the template
            try:
                soup.find('h1').decompose()
            except:
                # only entities have H1?
                pass
        
            hs = []
            # Renumber the headings
            for i in list(range(7))[::-1]:
                for h in soup.findAll('h%d' % i):
                    h.name = 'h%d' % (i + 2)
                    hs.append(h)
                
            # Change svg img references to embedded svg
            # because otherwise URLS are not interactive
            for img in soup.findAll("img"):
                if img['src'].endswith('.svg'):
                    print(img['src'].split('/')[-1].split('.')[0])
                    entity, hash = img['src'].split('/')[-1].split('.')[0].split('_')
                    svg = BeautifulSoup(open(os.path.join('svgs', entity + "_" + hash + '.dot.svg')))
                    img.replaceWith(svg.find('svg'))
                else:
                    img['src'] = img['src'][9:]
        
            html = str(soup)
        
    return render_template('entity.html', navigation=navigation_entries, content=html, number=idx, entity=resource, path=md[5:])

@app.route(make_url('listing'))
@app.route('/')
def listing():
    items = [{'number': name_to_number[n], 'url': url_for('resource', resource=n), 'title': n} for n in sorted(entity_names + type_names)]
    return render_template('list.html', navigation=navigation_entries, items=items)
    
@app.route(make_url('chapter-<n>/'))
def chapter(n):
    try: n = int(n)
    except: pass
    
    md_root = "data/docs/schemas"
    chp = chapter_lookup(number=n)
    t = chp.get('title')
    cat = t.split(" ")[0].lower()
    
    fn = os.path.join(md_root, cat, "README.md")
    
    if os.path.exists(fn):
        html = markdown.markdown(open(fn).read())
        soup = BeautifulSoup(html)
        # First h1 is handled by the template
        soup.find('h1').decompose()
        html = str(soup)
    else:
    	html = ''
    
    subs = [itms for t, itms in hierarchy if t == chp.get('title')][0]
    subs = list(map(operator.itemgetter(0), subs))
    
    return render_template('chapter.html', navigation=navigation_entries, content=html, path=fn[5:], title=t, number=n, subs=subs)
    
@app.route(make_url('annex-a.html'))
def annex_a():
    url = "https://github.com/buildingSMART/IFC4.3.x-output/blob/master/IFC.exp"
    html = "<h2>Computer interpretable listings</h2>" + \
        "<p>This annex contains a listing of the complete schema combining all definitions of clauses 5, 6, 7, and 8 without comments " + \
        "or other explanatory text. These listings are available in computer-interpretable form that may be parsed by computer.</p>" + \
        "<p>Official schema publications for this release are at the following URLs:</p>" + \
        (tabulate.tabulate([["IFC EXPRESS long form schema", '%s']], headers=["Format", "URL"], tablefmt='html') % \
            ("<a href='%(url)s'>%(url)s</a>" % locals()))
    return render_template('chapter.html', navigation=navigation_entries, content=html, path=None, title="Annex A", number="", subs=[])


@app.route(make_url('annex-c.html'))
def annex_c():
    html = "<h2>Inheritance listings</h2>" + \
        "<p>This annex contains listings of entity definitions organized by inheritance.</p>"
    
    def transform(s):
        s = s.strip('\n')
        padding = s.count(' ')
        entity = "".join([c for c in s if c != ' '])
        return '<tr><td>' + '&nbsp;' * padding * 4 + "<a href='" + url_for('resource', resource=entity) + "'>" + entity + "</a> </td><td>" + name_to_number[entity] + "</td>"
    
    html += "<table style='width:fit-content'>" +  "".join(map(transform, open("inheritance_listing.txt"))) + "</table>"
        
    return render_template('chapter.html', navigation=navigation_entries, content=html, path=None, title="Annex C", number="", subs=[])


@app.route(make_url('<name>/content.html'))
def schema(name):
    md_root = "data/docs/schemas"
    
    cat_full, schemas = [(t, itms) for t, itms in hierarchy if name in [i[0].lower() for i in itms]][0]
    cat = cat_full.split(" ")[0].lower()
    t, subs = [x for x in schemas if x[0].lower() == name][0]
    chp = chapter_lookup(cat=cat)
    
    n1 = chp.get('number')
    n2 = [s[0] for s in schemas].index(t) + 1
    n = "%d.%d" % (n1, n2)
    fn = os.path.join(md_root, cat, t, "README.md")
    
    if os.path.exists(fn):
        html = markdown.markdown(open(fn).read())
        soup = BeautifulSoup(html)
        # First h1 is handled by the template
        soup.find('h1').decompose()
        html = "<h2>" + n + ".1 Schema Definition</h2>" + str(soup)
    else:
        html = ''

    order = ["Types", "Entities"]
    subs = sorted(subs.items(), key=lambda tup: order.index(tup[0]))

    return render_template('chapter.html', navigation=navigation_entries, content=html, path=fn[5:], title=t, number=n, subs=subs)

@app.route('/search', methods=['GET', 'POST'])
def search():
    matches = []
    query = ''
    if request.method == 'POST' and request.form['query']:
        solr = pysolr.Solr('http://localhost:8983/solr/ifc')
        query = request.form['query']
        results = solr.search('body:(%s)' % query, **{'hl':'on', 'hl.fl':'body'})
        h = results.highlighting
        def format(s):
            return re.sub(r'[^\w\s<>/]', '', s)
        matches = [{
            'url': url_for('resource', resource=r['title'][0]), 
            'match': format(h[r['id']]['body'][0]),
            'title': r['title'][0]
        } for r in list(results)[0:10]]
    return render_template('search.html', navigation=navigation_entries, matches=matches, query=query)
