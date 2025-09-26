
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
# Prefer Postgres via DATABASE_URL. If missing (local dev), use SQLite in a writable dir.
db_url = os.environ.get('DATABASE_URL')
if not db_url:
    # Choose a writable path. Try /var/papaks (works if a Disk is mounted), else /tmp/papaks.
    candidates = ['/var/papaks', '/tmp/papaks']
    chosen = None
    for d in candidates:
        try:
            os.makedirs(d, exist_ok=True)
            # sanity write test
            testfile = os.path.join(d, '.write_test')
            with open(testfile, 'w') as f: f.write('ok')
            os.remove(testfile)
            chosen = d
            break
        except Exception:
            continue
    if not chosen:
        # last resort: current working directory
        chosen = os.getcwd()
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
            raise ValueError('Portion unit must match batch yield unit type (weight/volume).')
        portion_in_yield_unit = convert(self.portion_qty, self.portion_unit, self.batch.yield_unit)
        return (portion_in_yield_unit / (self.batch.yield_qty or 1.0)) * self.batch.total_cost

# Auth
STAFF_USER = os.environ.get('STAFF_USERNAME', 'admin')
STAFF_PASS = os.environ.get('STAFF_PASSWORD', 'changeme')

@app.before_request
def _ensure_tables_and_auth():
    if not getattr(app, "_tables_created", False):
        with app.app_context():
            db.create_all()
        app._tables_created = True
        # seed once if empty
        maybe_seed()
    from flask import request
    if request.endpoint in ('login','static'): 
        return
    if not session.get('auth_ok'):
        from flask import redirect
        return redirect(url_for('login'))

@app.route('/login', methods=['GET','POST'])
def login():
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
    return redirect(url_for('login'))

def maybe_seed():
    if InventoryItem.query.first(): return
    rice = InventoryItem(name='Rice', unit='g', unit_cost=0.003)
    chicken = InventoryItem(name='Chicken', unit='lb', unit_cost=2.50)
    oil = InventoryItem(name='Olive Oil', unit='ml', unit_cost=0.004)
    db.session.add_all([rice, chicken, oil]); db.session.commit()
    shawarma = BatchRecipe(name='Chicken Shawarma Marinade', yield_qty=18144, yield_unit='g', notes='~40 lb batch')
    db.session.add(shawarma); db.session.commit()
    db.session.add_all([
        BatchIngredient(batch_id=shawarma.id, item_id=chicken.id, qty=40, unit='lb'),
        BatchIngredient(batch_id=shawarma.id, item_id=oil.id, qty=500, unit='ml'),
    ]); db.session.commit()
    plate = MenuItem(name='Shawarma Plate', price=16.99, notes='Includes rice')
    db.session.add(plate); db.session.commit()
    db.session.add_all([
        MenuIngredient(menu_id=plate.id, item_id=rice.id, qty=180, unit='g'),
        MenuBatchPortion(menu_id=plate.id, batch_id=shawarma.id, portion_qty=180, portion_unit='g'),
    ]); db.session.commit()

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

@app.route('/inventory/delete/<int:item_id>')
def delete_inventory(item_id):
    obj = InventoryItem.query.get_or_404(item_id)
    db.session.delete(obj); db.session.commit()
    flash('Deleted.')
    return redirect(url_for('inventory'))

@app.route('/batches', methods=['GET','POST'])
def batches():
    if request.method == 'POST':
        name = request.form['name'].strip()
        yield_qty = float(request.form['yield_qty'])
        yield_unit = request.form['yield_unit'].strip().lower()
        notes = request.form.get('notes','')
        if yield_unit not in ('g','kg','oz','lb','ml','l'):
            flash('Yield unit must be one of g, kg, oz, lb, ml, l')
        else:
            db.session.add(BatchRecipe(name=name, yield_qty=yield_qty, yield_unit=yield_unit, notes=notes))
            db.session.commit()
            flash('Batch created.')
        return redirect(url_for('batches'))
    batches_ = BatchRecipe.query.order_by(BatchRecipe.name).all()
    return render_template('batches.html', batches=batches_, db_url=db_url)

@app.route('/batches/<int:batch_id>')
def edit_batch(batch_id):
    batch = BatchRecipe.query.get_or_404(batch_id)
    inventory = InventoryItem.query.order_by(InventoryItem.name).all()
    ingredients = BatchIngredient.query.filter_by(batch_id=batch.id).all()
    return render_template('edit_batch.html', batch=batch, inventory=inventory, ingredients=ingredients, db_url=db_url)

@app.route('/batches/<int:batch_id>/add', methods=['POST'])
def add_batch_ingredient(batch_id):
    batch = BatchRecipe.query.get_or_404(batch_id)
    item_id = int(request.form['item_id'])
    qty = float(request.form['qty'])
    unit = request.form['unit'].strip().lower()
    db.session.add(BatchIngredient(batch_id=batch.id, item_id=item_id, qty=qty, unit=unit))
    db.session.commit()
    flash('Ingredient added.')
    return redirect(url_for('edit_batch', batch_id=batch.id))

@app.route('/batches/ingredient/<int:ing_id>/delete')
def delete_batch_ingredient(ing_id):
    ing = BatchIngredient.query.get_or_404(ing_id)
    batch_id = ing.batch_id
    db.session.delete(ing); db.session.commit()
    flash('Ingredient deleted.')
    return redirect(url_for('edit_batch', batch_id=batch_id))

@app.route('/menu', methods=['GET','POST'])
def menu_items():
    if request.method == 'POST':
        name = request.form['name'].strip()
        price = float(request.form['price'])
        notes = request.form.get('notes','')
        db.session.add(MenuItem(name=name, price=price, notes=notes))
        db.session.commit()
        flash('Menu item created.')
        return redirect(url_for('menu_items'))
    menu = MenuItem.query.order_by(MenuItem.name).all()
    return render_template('menu.html', menu=menu, db_url=db_url)

@app.route('/menu/<int:menu_id>')
def edit_menu_item(menu_id):
    m = MenuItem.query.get_or_404(menu_id)
    inventory = InventoryItem.query.order_by(InventoryItem.name).all()
    batches = BatchRecipe.query.order_by(BatchRecipe.name).all()
    comps = []
    for c in m.inv_components:
        comps.append({'type':'inventory','id':c.id,'name':c.item.name,'qty':c.qty,'unit':c.unit,'ext_cost':c.ext_cost})
    for b in m.batch_portions:
        comps.append({'type':'batch','id':b.id,'name':b.batch.name,'qty':b.portion_qty,'unit':b.portion_unit,'ext_cost':b.ext_cost})
    return render_template('edit_menu.html', menu_item=m, inventory=inventory, batches=batches, components=comps, total_cost=m.cost, db_url=db_url)

@app.route('/menu/<int:menu_id>/add-inv', methods=['POST'])
def add_menu_ingredient(menu_id):
    m = MenuItem.query.get_or_404(menu_id)
    item_id = int(request.form['item_id'])
    qty = float(request.form['qty'])
    unit = request.form['unit'].strip().lower()
    db.session.add(MenuIngredient(menu_id=m.id, item_id=item_id, qty=qty, unit=unit))
    db.session.commit()
    flash('Inventory component added.')
    return redirect(url_for('edit_menu_item', menu_id=m.id))

@app.route('/menu/<int:menu_id>/add-batch', methods=['POST'])
def add_menu_batch_portion(menu_id):
    m = MenuItem.query.get_or_404(menu_id)
    batch_id = int(request.form['batch_id'])
    portion_qty = float(request.form['portion_qty'])
    portion_unit = request.form['portion_unit'].strip().lower()
    db.session.add(MenuBatchPortion(menu_id=m.id, batch_id=batch_id, portion_qty=portion_qty, portion_unit=portion_unit))
    db.session.commit()
    flash('Batch portion added.')
    return redirect(url_for('edit_menu_item', menu_id=m.id))

@app.route('/component/<kind>/<int:comp_id>/delete')
def delete_component(kind, comp_id):
    if kind == 'inventory':
        obj = MenuIngredient.query.get_or_404(comp_id); menu_id = obj.menu_id
    else:
        obj = MenuBatchPortion.query.get_or_404(comp_id); menu_id = obj.menu_id
    db.session.delete(obj); db.session.commit()
    flash('Deleted.')
    return redirect(url_for('edit_menu_item', menu_id=menu_id))

# CSV Export/Import
@app.route('/inventory/export.csv')
def inventory_export_csv():
    si = io.StringIO()
    w = csv.writer(si)
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

@app.route('/batches/export/recipes.csv')
def batches_export_recipes_csv():
    si = io.StringIO(); w = csv.writer(si)
    w.writerow(['name','yield_qty','yield_unit','notes'])
    for b in BatchRecipe.query.order_by(BatchRecipe.name).all():
        w.writerow([b.name, f"{b.yield_qty:.6f}", b.yield_unit, b.notes or ''])
    return Response(si.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition':'attachment; filename=batches_recipes.csv'})

@app.route('/batches/export/ingredients.csv')
def batches_export_ingredients_csv():
    si = io.StringIO(); w = csv.writer(si)
    w.writerow(['batch_name','item_name','qty','unit'])
    q = db.session.query(BatchRecipe, BatchIngredient, InventoryItem)\
        .join(BatchIngredient, BatchIngredient.batch_id==BatchRecipe.id)\
        .join(InventoryItem, InventoryItem.id==BatchIngredient.item_id)\
        .order_by(BatchRecipe.name, InventoryItem.name)
    for b, ing, item in q:
        w.writerow([b.name, item.name, f"{ing.qty:.6f}", ing.unit])
    return Response(si.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition':'attachment; filename=batches_ingredients.csv'})

@app.route('/batches/import.csv', methods=['POST'])
def batches_import_csv():
    rec_file = request.files.get('recipes')
    ing_file = request.files.get('ingredients')
    new_b=upd_b=new_i=0
    if rec_file and rec_file.filename.endswith('.csv'):
        text = rec_file.stream.read().decode('utf-8', errors='replace')
        r = csv.DictReader(io.StringIO(text))
        for row in r:
            name = (row.get('name') or '').strip()
            if not name: continue
            try:
                yield_qty = float(row.get('yield_qty') or 0)
            except Exception:
                yield_qty = 0.0
            yield_unit = (row.get('yield_unit') or '').strip().lower() or 'g'
            notes = (row.get('notes') or '').strip()
            obj = BatchRecipe.query.filter_by(name=name).first()
            if obj:
                obj.yield_qty = yield_qty or obj.yield_qty
                obj.yield_unit = yield_unit or obj.yield_unit
                obj.notes = notes
                upd_b += 1
            else:
                db.session.add(BatchRecipe(name=name, yield_qty=yield_qty, yield_unit=yield_unit, notes=notes)); new_b += 1
        db.session.commit()
    if ing_file and ing_file.filename.endswith('.csv'):
        text = ing_file.stream.read().decode('utf-8', errors='replace')
        r = csv.DictReader(io.StringIO(text))
        for row in r:
            bname = (row.get('batch_name') or '').strip()
            iname = (row.get('item_name') or '').strip()
            try:
                qty = float(row.get('qty') or 0)
            except Exception:
                qty = 0.0
            unit = (row.get('unit') or '').strip().lower() or 'g'
            if not bname or not iname: continue
            b = BatchRecipe.query.filter_by(name=bname).first()
            if not b:
                b = BatchRecipe(name=bname, yield_qty=1.0, yield_unit=unit)
                db.session.add(b); db.session.flush(); new_b += 1
            it = InventoryItem.query.filter_by(name=iname).first()
            if not it:
                it = InventoryItem(name=iname, unit=unit, unit_cost=0.0)
                db.session.add(it); db.session.flush(); new_i += 1
            db.session.add(BatchIngredient(batch_id=b.id, item_id=it.id, qty=qty, unit=unit))
        db.session.commit()
    flash(f'Batches import complete. New batches: {new_b}, updated batches: {upd_b}, new items from ingredients: {new_i}.')
    return redirect(url_for('batches'))

if __name__ == '__main__':
    app.run(debug=True)
