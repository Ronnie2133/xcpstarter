
from __future__ import annotations
import os, io, csv
from flask import Flask, render_template, request, redirect, url_for, flash, session, Response
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm import relationship
from dotenv import load_dotenv
from utils import convert, same_dimension, unit_cost_in

load_dotenv()
app = Flask(__name__, instance_relative_config=True)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret')

# --- Database config (Render-safe) ---
db_url = os.environ.get('DATABASE_URL')
if not db_url:
    candidates = ['/var/papaks', '/tmp/papaks']
    chosen = None
    for d in candidates:
        try:
            os.makedirs(d, exist_ok=True)
            testfile = os.path.join(d, '.write_test')
            with open(testfile, 'w') as f: f.write('ok')
            os.remove(testfile)
            chosen = d; break
        except Exception: continue
    if not chosen: chosen = os.getcwd()
    db_url = 'sqlite:///' + os.path.join(chosen, 'app.db')

app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# Models
class InventoryItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    unit = db.Column(db.String(10), nullable=False)
    unit_cost = db.Column(db.Float, default=0.0)

class BatchRecipe(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), unique=True, nullable=False)
    yield_qty = db.Column(db.Float, nullable=False)
    yield_unit = db.Column(db.String(10), nullable=False)
    notes = db.Column(db.Text, default='')
    ingredients = relationship('BatchIngredient', backref='batch', cascade='all, delete-orphan')
    @property
    def total_cost(self) -> float:
        return sum(ing.ext_cost for ing in self.ingredients)

class BatchIngredient(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    batch_id = db.Column(db.Integer, db.ForeignKey('batch_recipe.id'), nullable=False)
    item_id = db.Column(db.Integer, db.ForeignKey('inventory_item.id'), nullable=False)
    qty = db.Column(db.Float, nullable=False)
    unit = db.Column(db.String(10), nullable=False)
    item = relationship('InventoryItem')
    @property
    def ext_cost(self) -> float:
        return unit_cost_in(self.item.unit_cost, self.item.unit, self.unit) * self.qty

class MenuItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), unique=True, nullable=False)
    price = db.Column(db.Float, default=0.0)
    notes = db.Column(db.Text, default='')
    inv_components = relationship('MenuIngredient', backref='menu', cascade='all, delete-orphan')
    batch_portions = relationship('MenuBatchPortion', backref='menu', cascade='all, delete-orphan')
    @property
    def cost(self) -> float:
        inv_cost = sum(c.ext_cost for c in self.inv_components)
        batch_cost = sum(bp.ext_cost for bp in self.batch_portions)
        return inv_cost + batch_cost

class MenuIngredient(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    menu_id = db.Column(db.Integer, db.ForeignKey('menu_item.id'), nullable=False)
    item_id = db.Column(db.Integer, db.ForeignKey('inventory_item.id'), nullable=False)
    qty = db.Column(db.Float, nullable=False)
    unit = db.Column(db.String(10), nullable=False)
    item = relationship('InventoryItem')
    @property
    def ext_cost(self) -> float:
        return unit_cost_in(self.item.unit_cost, self.item.unit, self.unit) * self.qty

class MenuBatchPortion(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    menu_id = db.Column(db.Integer, db.ForeignKey('menu_item.id'), nullable=False)
    batch_id = db.Column(db.Integer, db.ForeignKey('batch_recipe.id'), nullable=False)
    portion_qty = db.Column(db.Float, nullable=False)
    portion_unit = db.Column(db.String(10), nullable=False)
    batch = relationship('BatchRecipe')
    @property
    def ext_cost(self) -> float:
        if not same_dimension(self.portion_unit, self.batch.yield_unit):
            raise ValueError('Portion unit must match batch yield unit type.')
        portion_in_yield_unit = convert(self.portion_qty, self.portion_unit, self.batch.yield_unit)
        return (portion_in_yield_unit / (self.batch.yield_qty or 1.0)) * self.batch.total_cost

# Auth via env
STAFF_USER = os.environ.get('STAFF_USERNAME', None)
STAFF_PASS = os.environ.get('STAFF_PASSWORD', None)

@app.before_request
def _ensure_tables_and_auth():
    if not getattr(app, "_tables_created", False):
        with app.app_context():
            db.create_all()
        app._tables_created = True
        maybe_seed()
    from flask import request
    if request.endpoint in ('login','static'): 
        return
    # if STAFF env vars are set, require login; else app is open (dev convenience)
    if STAFF_USER and STAFF_PASS:
        if not session.get('auth_ok'):
            from flask import redirect
            return redirect(url_for('login'))

@app.route('/login', methods=['GET','POST'])
def login():
    if not (STAFF_USER and STAFF_PASS):
        flash('Login not configured; set STAFF_USERNAME and STAFF_PASSWORD.')
        return redirect(url_for('index'))
    if request.method == 'POST':
        u = request.form.get('username','').strip()
        p = request.form.get('password','').strip()
        if u == STAFF_USER and p == STAFF_PASS:
            session['auth_ok'] = True
            flash('Welcome.')
            return redirect(url_for('index'))
        flash('Invalid credentials.')
    return render_template('login.html', db_url=db_url)

@app.route('/logout')
def logout():
    session.clear()
    flash('Signed out.')
    return redirect(url_for('login' if (STAFF_USER and STAFF_PASS) else 'index'))

def maybe_seed():
    if InventoryItem.query.first(): return
    db.session.add_all([InventoryItem(name='Rice', unit='g', unit_cost=0.003),
                        InventoryItem(name='Chicken', unit='lb', unit_cost=2.50),
                        InventoryItem(name='Olive Oil', unit='ml', unit_cost=0.004)])
    db.session.commit()

# Routes
@app.route('/')
def index():
    menu = MenuItem.query.all()
    return render_template('index.html', menu=menu, db_url=db_url)

@app.route('/inventory', methods=['GET','POST'])
def inventory():
    if request.method == 'POST':
        name = request.form['name'].strip()
        unit = request.form['unit'].strip().lower()
        unit_cost = float(request.form.get('unit_cost') or 0)
        if unit not in ('g','kg','oz','lb','ml','l'):
            flash('Unit must be one of g, kg, oz, lb, ml, l')
        else:
            db.session.add(InventoryItem(name=name, unit=unit, unit_cost=unit_cost))
            db.session.commit()
            flash('Inventory item added.')
        return redirect(url_for('inventory'))
    items = InventoryItem.query.order_by(InventoryItem.name).all()
    return render_template('inventory.html', items=items, db_url=db_url)

@app.route('/inventory/<int:item_id>/update', methods=['POST'])
def update_inventory(item_id):
    obj = InventoryItem.query.get_or_404(item_id)
    name = request.form.get('name','').strip()
    unit = request.form.get('unit','').strip().lower()
    try:
        unit_cost = float(request.form.get('unit_cost') or 0)
    except Exception:
        unit_cost = obj.unit_cost
    if not name:
        flash('Name is required.'); return redirect(url_for('inventory'))
    if unit not in ('g','kg','oz','lb','ml','l'):
        flash('Unit must be one of g, kg, oz, lb, ml, l'); return redirect(url_for('inventory'))
    exists = InventoryItem.query.filter(InventoryItem.name==name, InventoryItem.id!=obj.id).first()
    if exists:
        flash('Another item already uses that name.'); return redirect(url_for('inventory'))
    obj.name = name; obj.unit = unit; obj.unit_cost = unit_cost
    db.session.commit()
    flash('Inventory item updated.')
    return redirect(url_for('inventory'))

@app.route('/inventory/delete/<int:item_id>')
def delete_inventory(item_id):
    obj = InventoryItem.query.get_or_404(item_id)
    db.session.delete(obj); db.session.commit()
    flash('Deleted.')
    return redirect(url_for('inventory'))

# Minimal stubs for other pages so template extends work
@app.route('/batches')
def batches():
    batches_ = BatchRecipe.query.order_by(BatchRecipe.name).all()
    return render_template('batches.html', batches=batches_, db_url=db_url)

@app.route('/menu')
def menu_items():
    menu = MenuItem.query.order_by(MenuItem.name).all()
    return render_template('menu.html', menu=menu, db_url=db_url)

# CSV Export/Import (inventory only for brevity in this patch)
@app.route('/inventory/export.csv')
def inventory_export_csv():
    si = io.StringIO(); w = csv.writer(si)
    w.writerow(['name','unit','unit_cost'])
    for i in InventoryItem.query.order_by(InventoryItem.name).all():
        w.writerow([i.name, i.unit, f"{i.unit_cost:.6f}"])
    return Response(si.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition':'attachment; filename=inventory.csv'})

@app.route('/inventory/import.csv', methods=['POST'])
def inventory_import_csv():
    f = request.files.get('file')
    if not f or not f.filename.endswith('.csv'):
        flash('Please upload a .csv file.'); return redirect(url_for('inventory'))
    count=0; updated=0
    text = f.stream.read().decode('utf-8', errors='replace')
    r = csv.DictReader(io.StringIO(text))
    for row in r:
        name = (row.get('name') or '').strip()
        unit = (row.get('unit') or '').strip().lower()
        try:
            unit_cost = float(row.get('unit_cost') or 0)
        except Exception:
            unit_cost = 0.0
        if not name or unit not in ('g','kg','oz','lb','ml','l'):
            continue
        obj = InventoryItem.query.filter_by(name=name).first()
        if obj:
            obj.unit = unit; obj.unit_cost = unit_cost; updated += 1
        else:
            db.session.add(InventoryItem(name=name, unit=unit, unit_cost=unit_cost)); count += 1
    db.session.commit()
    flash(f'Imported {count} new, updated {updated}.')
    return redirect(url_for('inventory'))

if __name__ == '__main__':
    app.run(debug=True)
