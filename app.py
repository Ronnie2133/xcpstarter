from __future__ import annotations
import os, io, csv, json, urllib.request, urllib.parse
from flask import (
    Flask, render_template, request, redirect, url_for, flash, session, Response, jsonify
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm import relationship
from dotenv import load_dotenv
from utils import convert, same_dimension, unit_cost_in

load_dotenv()
app = Flask(__name__, instance_relative_config=True)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret')

db_url = os.environ.get('DATABASE_URL', '')

if db_url.startswith('postgres://'):
    db_url = db_url.replace('postgres://', 'postgresql+psycopg://', 1)
elif db_url.startswith('postgresql://'):
    db_url = db_url.replace('postgresql://', 'postgresql+psycopg://', 1)
if not db_url:
    candidates = ['/var/papaks', '/tmp/papaks']
    chosen = None
    for d in candidates:
        try:
            os.makedirs(d, exist_ok=True)
            testfile = os.path.join(d, '.write_test')
            with open(testfile, 'w') as f:
                f.write('ok')
            os.remove(testfile)
            chosen = d
            break
        except Exception:
            continue
    if not chosen:
        chosen = os.getcwd()
    db_url = 'sqlite:///' + os.path.join(chosen, 'app.db')

app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# ---------- Models ----------

class InventoryItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    unit = db.Column(db.String(10), nullable=False)
    unit_cost = db.Column(db.Float, default=0.0)
    category = db.Column(db.String(60), default='Other')
    calories_per_100g = db.Column(db.Float, default=0.0)

class BatchRecipe(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), unique=True, nullable=False)
    raw_qty = db.Column(db.Float, nullable=True)
    yield_qty = db.Column(db.Float, nullable=False)
    yield_unit = db.Column(db.String(10), nullable=False)
    notes = db.Column(db.Text, default='')
    allergens = db.Column(db.String(300), default='')
    prep_time_min = db.Column(db.Integer, default=0)
    ingredients = relationship('BatchIngredient', backref='batch', cascade='all, delete-orphan')

    @property
    def total_cost(self) -> float:
        inv_cost = sum(ing.ext_cost for ing in self.ingredients)
        sub_cost = 0.0
        for sb in getattr(self, 'sub_batches', []):
            try:
                sub_cost += sb.ext_cost
            except Exception:
                continue
        return inv_cost + sub_cost

    @property
    def total_calories(self) -> float:
        """Total calories in the entire batch yield."""
        cal = 0.0
        for ing in self.ingredients:
            cal += ing.ext_calories
        for sb in getattr(self, 'sub_batches', []):
            try:
                cal += sb.ext_calories
            except Exception:
                continue
        return cal

    @property
    def calories_per_yield_unit(self) -> float:
        if not self.yield_qty or self.yield_qty == 0:
            return 0.0
        return self.total_calories / self.yield_qty

    @property
    def yield_percent(self):
        if not self.raw_qty or self.raw_qty == 0:
            return None
        return (self.yield_qty / self.raw_qty) * 100

    @property
    def cost_per_yield_unit(self):
        if not self.yield_qty or self.yield_qty == 0:
            return 0
        return self.total_cost / self.yield_qty

    @property
    def is_low_yield(self):
        yp = self.yield_percent
        if yp is None:
            return False
        return yp < 70

class BatchSubBatch(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    parent_id = db.Column(db.Integer, db.ForeignKey('batch_recipe.id'), nullable=False)
    child_id  = db.Column(db.Integer, db.ForeignKey('batch_recipe.id'), nullable=False)
    qty       = db.Column(db.Float, nullable=False)
    unit      = db.Column(db.String(10), nullable=False)

    parent = relationship('BatchRecipe', foreign_keys=[parent_id], backref='sub_batches')
    child  = relationship('BatchRecipe', foreign_keys=[child_id])

    @property
    def ext_cost(self) -> float:
        if not self.child or (self.child.yield_qty or 0) <= 0:
            return 0.0
        if not same_dimension(self.unit, self.child.yield_unit):
            return 0.0
        portion_in_yield_unit = convert(self.qty, self.unit, self.child.yield_unit)
        return (portion_in_yield_unit / self.child.yield_qty) * (self.child.total_cost or 0.0)

    @property
    def ext_calories(self) -> float:
        if not self.child or (self.child.yield_qty or 0) <= 0:
            return 0.0
        if not same_dimension(self.unit, self.child.yield_unit):
            return 0.0
        portion_in_yield_unit = convert(self.qty, self.unit, self.child.yield_unit)
        return (portion_in_yield_unit / self.child.yield_qty) * self.child.total_calories

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

    @property
    def ext_calories(self) -> float:
        """Calories contributed by this ingredient line."""
        if not self.item.calories_per_100g:
            return 0.0
        # Convert qty to grams
        try:
            qty_in_g = convert(self.qty, self.unit, 'g')
        except Exception:
            return 0.0
        return (qty_in_g / 100.0) * self.item.calories_per_100g

class MenuItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), unique=True, nullable=False)
    price = db.Column(db.Float, default=0.0)
    notes = db.Column(db.Text, default='')
    category = db.Column(db.String(60), default='Other')
    inv_components = relationship('MenuIngredient', backref='menu', cascade='all, delete-orphan')
    batch_portions = relationship('MenuBatchPortion', backref='menu', cascade='all, delete-orphan')

    @property
    def cost(self) -> float:
        inv_cost = sum(c.ext_cost for c in self.inv_components)
        batch_cost = sum(bp.ext_cost for bp in self.batch_portions)
        return inv_cost + batch_cost

    @property
    def total_calories(self) -> float:
        inv_cal = sum(c.ext_calories for c in self.inv_components)
        batch_cal = sum(bp.ext_calories for bp in self.batch_portions)
        return inv_cal + batch_cal

    @property
    def food_cost_pct(self):
        if self.price and self.price > 0:
            return (self.cost / self.price) * 100
        return None

    @property
    def profit(self):
        return self.price - self.cost if self.price else None

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

    @property
    def ext_calories(self) -> float:
        if not self.item.calories_per_100g:
            return 0.0
        try:
            qty_in_g = convert(self.qty, self.unit, 'g')
        except Exception:
            return 0.0
        return (qty_in_g / 100.0) * self.item.calories_per_100g

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
            return 0.0
        if (self.batch.yield_qty or 0) <= 0:
            return 0.0
        portion_in_yield_unit = convert(self.portion_qty, self.portion_unit, self.batch.yield_unit)
        return (portion_in_yield_unit / self.batch.yield_qty) * (self.batch.total_cost or 0.0)

    @property
    def ext_calories(self) -> float:
        if not same_dimension(self.portion_unit, self.batch.yield_unit):
            return 0.0
        if (self.batch.yield_qty or 0) <= 0:
            return 0.0
        portion_in_yield_unit = convert(self.portion_qty, self.portion_unit, self.batch.yield_unit)
        return (portion_in_yield_unit / self.batch.yield_qty) * self.batch.total_calories

# ---------- Auth ----------
STAFF_USER = os.environ.get('STAFF_USERNAME', None)
STAFF_PASS = os.environ.get('STAFF_PASSWORD', None)

def migrate_db():
    """Add any new columns that don't exist yet in the live database.
    Safe to run on every startup — skips columns that already exist."""
    migrations = [
        # (table_name, column_name, column_definition)
        ('inventory_item', 'category',          "VARCHAR(60) DEFAULT 'Other'"),
        ('inventory_item', 'calories_per_100g', 'FLOAT DEFAULT 0.0'),
        ('batch_recipe',   'allergens',          "VARCHAR(300) DEFAULT ''"),
        ('batch_recipe',   'prep_time_min',      'INTEGER DEFAULT 0'),
        ('menu_item',      'category',           "VARCHAR(60) DEFAULT 'Other'"),
    ]
    with db.engine.connect() as conn:
        for table, column, definition in migrations:
            try:
                conn.execute(db.text(
                    f'ALTER TABLE {table} ADD COLUMN {column} {definition}'
                ))
                conn.commit()
            except Exception:
                # Column already exists — safe to ignore
                conn.rollback()

@app.before_request
def _ensure_tables_and_auth():
    if not getattr(app, "_tables_created", False):
        with app.app_context():
            db.create_all()
            try:
                migrate_db()
            except Exception as e:
                print(f"Migration warning: {e}")
        app._tables_created = True
        try:
            maybe_seed()
        except Exception:
            pass

    if request.endpoint in ('login', 'static', 'usda_lookup'):
        return

    if STAFF_USER and STAFF_PASS:
        if not session.get('auth_ok'):
            return redirect(url_for('login'))

@app.route('/login', methods=['GET','POST'])
def login():
    if not (STAFF_USER and STAFF_PASS):
        flash('Login not configured.')
        return redirect(url_for('index'))
    if request.method == 'POST':
        u = request.form.get('username','').strip()
        p = request.form.get('password','').strip()
        if u == STAFF_USER and p == STAFF_PASS:
            session['auth_ok'] = True
            return redirect(url_for('index'))
        flash('Invalid credentials.')
    return render_template('login.html', db_url=db_url)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login' if (STAFF_USER and STAFF_PASS) else 'index'))

def maybe_seed():
    if InventoryItem.query.first():
        return
    seeds = [
        InventoryItem(name='Chicken Breast', unit='lb', unit_cost=2.50, category='Proteins', calories_per_100g=165),
        InventoryItem(name='Rice', unit='g', unit_cost=0.003, category='Dry Goods', calories_per_100g=130),
        InventoryItem(name='Olive Oil', unit='ml', unit_cost=0.004, category='Oils & Fats', calories_per_100g=884),
        InventoryItem(name='All-Purpose Flour', unit='g', unit_cost=0.002, category='Dry Goods', calories_per_100g=364),
        InventoryItem(name='Butter', unit='g', unit_cost=0.008, category='Dairy', calories_per_100g=717),
        InventoryItem(name='Heavy Cream', unit='ml', unit_cost=0.005, category='Dairy', calories_per_100g=340),
    ]
    db.session.add_all(seeds)
    db.session.commit()

# ---------- USDA Calorie Lookup API ----------
@app.route('/api/usda_lookup')
def usda_lookup():
    """Proxy USDA FoodData Central search and return calorie info."""
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify({'error': 'No query'}), 400

    api_key = os.environ.get('USDA_API_KEY', 'DEMO_KEY')
    params = urllib.parse.urlencode({
        'query': query,
        'dataType': 'Foundation,SR Legacy',
        'pageSize': 5,
        'api_key': api_key
    })
    url = f'https://api.nal.usda.gov/fdc/v1/foods/search?{params}'

    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    results = []
    for food in data.get('foods', []):
        cal = None
        for n in food.get('foodNutrients', []):
            if n.get('nutrientName') == 'Energy' and n.get('unitName') == 'KCAL':
                cal = n.get('value')
                break
        if cal is not None:
            results.append({
                'name': food.get('description', ''),
                'calories_per_100g': cal,
                'fdcId': food.get('fdcId')
            })

    return jsonify({'results': results})

# ---------- Inventory Routes ----------
@app.route('/inventory', methods=['GET','POST'])
def inventory():
    if request.method == 'POST':
        name = request.form['name'].strip()
        unit = request.form['unit'].strip().lower()
        unit_cost = float(request.form.get('unit_cost') or 0)
        category = request.form.get('category', 'Other').strip()
        calories_per_100g = float(request.form.get('calories_per_100g') or 0)
        if unit not in ('g','kg','oz','lb','ml','l','ct'):
            flash('Unit must be one of g, kg, oz, lb, ml, l')
        else:
            db.session.add(InventoryItem(
                name=name, unit=unit, unit_cost=unit_cost,
                category=category, calories_per_100g=calories_per_100g
            ))
            db.session.commit()
            flash(f'"{name}" added to inventory.')
        return redirect(url_for('inventory'))
    items = InventoryItem.query.order_by(InventoryItem.category, InventoryItem.name).all()
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
    try:
        calories_per_100g = float(request.form.get('calories_per_100g') or 0)
    except Exception:
        calories_per_100g = obj.calories_per_100g
    category = request.form.get('category', obj.category).strip()
    if not name:
        flash('Name is required.')
        return redirect(url_for('inventory'))
    if unit not in ('g','kg','oz','lb','ml','l'):
        flash('Unit must be one of g, kg, oz, lb, ml, l')
        return redirect(url_for('inventory'))
    exists = InventoryItem.query.filter(InventoryItem.name == name, InventoryItem.id != obj.id).first()
    if exists:
        flash('Another item already uses that name.')
        return redirect(url_for('inventory'))
    obj.name = name
    obj.unit = unit
    obj.unit_cost = unit_cost
    obj.category = category
    obj.calories_per_100g = calories_per_100g
    db.session.commit()
    flash(f'"{name}" updated.')
    return redirect(url_for('inventory'))

@app.route('/inventory/delete/<int:item_id>')
def delete_inventory(item_id):
    obj = InventoryItem.query.get_or_404(item_id)
    db.session.delete(obj)
    db.session.commit()
    flash('Item deleted.')
    return redirect(url_for('inventory'))

@app.route('/inventory/export.csv')
def inventory_export_csv():
    si = io.StringIO()
    w = csv.writer(si)
    w.writerow(['name','unit','unit_cost','category','calories_per_100g'])
    for i in InventoryItem.query.order_by(InventoryItem.name).all():
        w.writerow([i.name, i.unit, f"{i.unit_cost:.6f}", i.category or '', i.calories_per_100g or 0])
    return Response(si.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition':'attachment; filename=inventory.csv'})

@app.route('/inventory/import.csv', methods=['POST'])
def inventory_import_csv():
    f = request.files.get('file')
    if not f or not f.filename.endswith('.csv'):
        flash('Please upload a .csv file.')
        return redirect(url_for('inventory'))
    count = 0; updated = 0
    text = f.stream.read().decode('utf-8', errors='replace')
    for row in csv.DictReader(io.StringIO(text)):
        name = (row.get('name') or '').strip()
        unit = (row.get('unit') or '').strip().lower()
        try:
            unit_cost = float(row.get('unit_cost') or 0)
        except Exception:
            unit_cost = 0.0
        try:
            calories_per_100g = float(row.get('calories_per_100g') or 0)
        except Exception:
            calories_per_100g = 0.0
        category = (row.get('category') or 'Other').strip()
        if not name or unit not in ('g','kg','oz','lb','ml','l'):
            continue
        obj = InventoryItem.query.filter_by(name=name).first()
        if obj:
            obj.unit = unit; obj.unit_cost = unit_cost
            obj.category = category; obj.calories_per_100g = calories_per_100g
            updated += 1
        else:
            db.session.add(InventoryItem(name=name, unit=unit, unit_cost=unit_cost,
                                         category=category, calories_per_100g=calories_per_100g))
            count += 1
    db.session.commit()
    flash(f'Imported {count} new, updated {updated}.')
    return redirect(url_for('inventory'))

# ---------- Inventory Autocomplete API ----------
@app.route('/api/inventory_search')
def inventory_search():
    q = request.args.get('q', '').strip().lower()
    items = InventoryItem.query.order_by(InventoryItem.name).all()
    results = [
        {'id': i.id, 'name': i.name, 'unit': i.unit,
         'unit_cost': i.unit_cost, 'calories_per_100g': i.calories_per_100g or 0}
        for i in items if q in i.name.lower()
    ]
    return jsonify(results)

# ---------- Batch Routes ----------
@app.route('/batches', methods=['GET', 'POST'])
def batches():
    if request.method == 'POST':
        name = request.form.get('name','').strip()
        try:
            raw_qty_val = request.form.get('raw_qty')
            raw_qty = float(raw_qty_val) if raw_qty_val not in (None, '') else None
        except Exception:
            raw_qty = None
        try:
            yield_qty = float(request.form.get('yield_qty') or 0)
        except Exception:
            yield_qty = 0.0
        yield_unit = (request.form.get('yield_unit') or '').strip().lower()
        notes = (request.form.get('notes') or '').strip()
        allergens = (request.form.get('allergens') or '').strip()
        try:
            prep_time_min = int(request.form.get('prep_time_min') or 0)
        except Exception:
            prep_time_min = 0

        if not name or yield_unit not in ('g','kg','oz','lb','ml','l'):
            flash('Name and a valid yield unit are required.')
        elif BatchRecipe.query.filter_by(name=name).first():
            flash('Batch name already exists.')
        else:
            db.session.add(BatchRecipe(
                name=name, raw_qty=raw_qty, yield_qty=yield_qty,
                yield_unit=yield_unit, notes=notes,
                allergens=allergens, prep_time_min=prep_time_min
            ))
            db.session.commit()
            flash(f'Batch "{name}" created.')
        return redirect(url_for('batches'))

    batches_ = BatchRecipe.query.order_by(BatchRecipe.name).all()
    return render_template('batches.html', batches=batches_, db_url=db_url)

@app.route('/batches/<int:batch_id>/clone', methods=['POST'])
def batch_clone(batch_id):
    src = BatchRecipe.query.get_or_404(batch_id)
    base_name = f"Copy of {src.name}"
    # ensure unique
    suffix = 0
    new_name = base_name
    while BatchRecipe.query.filter_by(name=new_name).first():
        suffix += 1
        new_name = f"{base_name} ({suffix})"
    clone = BatchRecipe(
        name=new_name, raw_qty=src.raw_qty, yield_qty=src.yield_qty,
        yield_unit=src.yield_unit, notes=src.notes,
        allergens=src.allergens, prep_time_min=src.prep_time_min
    )
    db.session.add(clone)
    db.session.flush()
    for ing in src.ingredients:
        db.session.add(BatchIngredient(
            batch_id=clone.id, item_id=ing.item_id,
            qty=ing.qty, unit=ing.unit
        ))
    db.session.commit()
    flash(f'Cloned as "{new_name}".')
    return redirect(url_for('batch_detail', batch_id=clone.id))

@app.route('/batches/<int:batch_id>', methods=['GET', 'POST'])
def batch_detail(batch_id):
    b = BatchRecipe.query.get_or_404(batch_id)
    items = InventoryItem.query.order_by(InventoryItem.name).all()
    if request.method == 'POST':
        # Bulk add support: item_id[], qty[], unit[]
        item_ids = request.form.getlist('item_id[]')
        qtys = request.form.getlist('qty[]')
        units = request.form.getlist('unit[]')
        added = 0
        for item_id, qty, unit in zip(item_ids, qtys, units):
            try:
                item_id = int(item_id)
                qty = float(qty)
                unit = unit.strip().lower()
            except Exception:
                continue
            if unit not in ('g','kg','oz','lb','ml','l') or qty <= 0:
                continue
            db.session.add(BatchIngredient(batch_id=b.id, item_id=item_id, qty=qty, unit=unit))
            added += 1
        if added:
            db.session.commit()
            flash(f'{added} ingredient(s) added.')
        else:
            flash('No valid ingredients to add.')
        return redirect(url_for('batch_detail', batch_id=b.id))

    all_batches = BatchRecipe.query.order_by(BatchRecipe.name).all()
    return render_template('batch_detail.html', b=b, items=items, all_batches=all_batches, db_url=db_url)

@app.post('/batches/<int:batch_id>/ingredient/<int:ing_id>/update')
def batch_update_ingredient(batch_id, ing_id):
    ing = BatchIngredient.query.get_or_404(ing_id)
    try:
        ing.qty = float(request.form.get('qty', ing.qty) or 0)
    except Exception:
        pass
    unit = (request.form.get('unit') or ing.unit).strip().lower()
    if unit in ('g','kg','oz','lb','ml','l'):
        ing.unit = unit
    db.session.commit()
    flash('Ingredient updated.')
    return redirect(url_for('batch_detail', batch_id=batch_id))

@app.post('/batches/<int:batch_id>/subbatch/<int:sb_id>/update')
def batch_update_subbatch(batch_id, sb_id):
    sb = BatchSubBatch.query.get_or_404(sb_id)
    try:
        sb.qty = float(request.form.get('qty', sb.qty) or 0)
    except Exception:
        pass
    unit = (request.form.get('unit') or sb.unit).strip().lower()
    if unit in ('g','kg','oz','lb','ml','l'):
        sb.unit = unit
    db.session.commit()
    flash('Sub-batch updated.')
    return redirect(url_for('batch_detail', batch_id=batch_id))

@app.route('/batches/<int:batch_id>/delete_ing/<int:ing_id>')
def batch_delete_ing(batch_id, ing_id):
    ing = BatchIngredient.query.get_or_404(ing_id)
    db.session.delete(ing)
    db.session.commit()
    flash('Ingredient removed.')
    return redirect(url_for('batch_detail', batch_id=batch_id))

@app.route('/batches/export.csv')
def batches_export_csv():
    si = io.StringIO()
    w = csv.writer(si)
    w.writerow(['batch_name','raw_qty','yield_qty','yield_unit','notes','allergens','prep_time_min','component_type','component_name','qty','unit'])
    for b in BatchRecipe.query.order_by(BatchRecipe.name).all():
        for ing in b.ingredients:
            w.writerow([b.name, b.raw_qty or '', b.yield_qty, b.yield_unit, b.notes or '',
                        b.allergens or '', b.prep_time_min or 0, 'inventory', ing.item.name, ing.qty, ing.unit])
        for sb in getattr(b, 'sub_batches', []):
            w.writerow([b.name, b.raw_qty or '', b.yield_qty, b.yield_unit, b.notes or '',
                        b.allergens or '', b.prep_time_min or 0, 'batch', sb.child.name, sb.qty, sb.unit])
    return Response(si.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment; filename=batches.csv'})

@app.route('/batches/import.csv', methods=['POST'])
def batches_import_csv():
    f = request.files.get('file')
    if not f or not f.filename.endswith('.csv'):
        flash('Please upload a .csv file.')
        return redirect(url_for('batches'))
    text = f.stream.read().decode('utf-8', errors='replace')
    rows = list(csv.DictReader(io.StringIO(text)))
    from collections import defaultdict
    groups = defaultdict(list)
    for r in rows:
        name = (r.get('batch_name') or '').strip()
        if name:
            groups[name].append(r)
    created = 0; updated = 0; skipped = 0
    for bname, batch_rows in groups.items():
        first = batch_rows[0]
        try:
            raw_qty = float(first.get('raw_qty') or '') if (first.get('raw_qty') or '').strip() else None
        except Exception:
            raw_qty = None
        try:
            yield_qty = float(first.get('yield_qty') or 0)
        except Exception:
            yield_qty = 0
        yield_unit = (first.get('yield_unit') or '').strip().lower()
        notes = (first.get('notes') or '').strip()
        allergens = (first.get('allergens') or '').strip()
        try:
            prep_time_min = int(first.get('prep_time_min') or 0)
        except Exception:
            prep_time_min = 0
        if yield_qty <= 0 or yield_unit not in ('g','kg','oz','lb','ml','l'):
            skipped += 1; continue
        batch = BatchRecipe.query.filter_by(name=bname).first()
        if not batch:
            batch = BatchRecipe(name=bname, raw_qty=raw_qty, yield_qty=yield_qty,
                                yield_unit=yield_unit, notes=notes,
                                allergens=allergens, prep_time_min=prep_time_min)
            db.session.add(batch); db.session.flush(); created += 1
        else:
            batch.raw_qty = raw_qty; batch.yield_qty = yield_qty
            batch.yield_unit = yield_unit; batch.notes = notes
            batch.allergens = allergens; batch.prep_time_min = prep_time_min
            BatchIngredient.query.filter_by(batch_id=batch.id).delete()
            BatchSubBatch.query.filter_by(parent_id=batch.id).delete()
            updated += 1
        for r in batch_rows:
            ctype = (r.get('component_type') or '').strip().lower()
            cname = (r.get('component_name') or '').strip()
            try:
                qty = float(r.get('qty') or 0)
            except Exception:
                qty = 0
            unit = (r.get('unit') or '').strip().lower()
            if not cname or qty <= 0 or unit not in ('g','kg','oz','lb','ml','l'):
                continue
            if ctype == 'inventory':
                inv = InventoryItem.query.filter_by(name=cname).first()
                if not inv:
                    inv = InventoryItem(name=cname, unit=unit, unit_cost=0.0)
                    db.session.add(inv); db.session.flush()
                db.session.add(BatchIngredient(batch_id=batch.id, item_id=inv.id, qty=qty, unit=unit))
            elif ctype == 'batch':
                child = BatchRecipe.query.filter_by(name=cname).first()
                if not child:
                    child = BatchRecipe(name=cname, raw_qty=None, yield_qty=1.0,
                                        yield_unit=unit, notes='(placeholder)')
                    db.session.add(child); db.session.flush()
                db.session.add(BatchSubBatch(parent_id=batch.id, child_id=child.id, qty=qty, unit=unit))
    db.session.commit()
    flash(f'Import complete. Created {created}, updated {updated}, skipped {skipped}.')
    return redirect(url_for('batches'))

@app.route('/batches/<int:batch_id>/add_subbatch', methods=['POST'])
def batch_add_subbatch(batch_id):
    parent = BatchRecipe.query.get_or_404(batch_id)
    try:
        child_id = int(request.form['child_id'])
        qty = float(request.form['qty'])
        unit = (request.form['unit'] or '').strip().lower()
    except Exception:
        flash('Select a batch and enter a valid portion.')
        return redirect(url_for('batch_detail', batch_id=batch_id))
    if unit not in ('g','kg','oz','lb','ml','l'):
        flash('Unit must be one of g, kg, oz, lb, ml, l.')
        return redirect(url_for('batch_detail', batch_id=batch_id))
    if child_id == parent.id:
        flash('A batch cannot include itself.')
        return redirect(url_for('batch_detail', batch_id=batch_id))
    child = BatchRecipe.query.get_or_404(child_id)
    db.session.add(BatchSubBatch(parent_id=parent.id, child_id=child.id, qty=qty, unit=unit))
    db.session.commit()
    flash('Sub-batch added.')
    return redirect(url_for('batch_detail', batch_id=batch_id))

@app.route('/batches/<int:batch_id>/delete_subbatch/<int:sb_id>')
def batch_delete_subbatch(batch_id, sb_id):
    sb = BatchSubBatch.query.get_or_404(sb_id)
    db.session.delete(sb)
    db.session.commit()
    flash('Sub-batch removed.')
    return redirect(url_for('batch_detail', batch_id=batch_id))

@app.route('/batches/<int:batch_id>/update', methods=['POST'])
def batch_update(batch_id):
    b = BatchRecipe.query.get_or_404(batch_id)
    allowed = {'g','kg','oz','lb','ml','l'}
    name = (request.form.get('name') or b.name).strip()
    notes = (request.form.get('notes') or '').strip()
    allergens = (request.form.get('allergens') or '').strip()
    try:
        prep_time_min = int(request.form.get('prep_time_min') or 0)
    except Exception:
        prep_time_min = b.prep_time_min
    raw_qty_raw = request.form.get('raw_qty')
    try:
        raw_qty = float(raw_qty_raw) if raw_qty_raw not in (None, '') else None
    except Exception:
        raw_qty = b.raw_qty
    try:
        yield_qty = float(request.form.get('yield_qty') or b.yield_qty)
    except Exception:
        yield_qty = b.yield_qty
    yield_unit = (request.form.get('yield_unit') or b.yield_unit).strip().lower()
    if not name or yield_qty <= 0 or yield_unit not in allowed:
        flash('Please provide a valid name, positive yield, and a valid unit.')
        return redirect(url_for('batch_detail', batch_id=b.id))
    exists = BatchRecipe.query.filter(BatchRecipe.name == name, BatchRecipe.id != b.id).first()
    if exists:
        flash('Another batch already uses that name.')
        return redirect(url_for('batch_detail', batch_id=b.id))
    b.name = name; b.raw_qty = raw_qty; b.yield_qty = yield_qty
    b.yield_unit = yield_unit; b.notes = notes
    b.allergens = allergens; b.prep_time_min = prep_time_min
    db.session.commit()
    flash('Batch updated.')
    return redirect(url_for('batch_detail', batch_id=b.id))

@app.route('/batches/<int:batch_id>/delete', methods=['POST'])
def batch_delete(batch_id):
    b = BatchRecipe.query.get_or_404(batch_id)
    BatchIngredient.query.filter_by(batch_id=b.id).delete()
    BatchSubBatch.query.filter_by(parent_id=b.id).delete()
    BatchSubBatch.query.filter_by(child_id=b.id).delete()
    db.session.delete(b)
    db.session.commit()
    flash('Batch deleted.')
    return redirect(url_for('batches'))

# ---------- Menu Routes ----------
@app.route('/')
def index():
    menu = MenuItem.query.all()
    batches_ = BatchRecipe.query.all()
    inv_items = InventoryItem.query.all()

    # Dashboard stats
    total_items = len(menu)
    avg_food_cost = None
    priced = [m for m in menu if m.price and m.price > 0]
    if priced:
        avg_food_cost = sum(m.food_cost_pct for m in priced if m.food_cost_pct is not None) / len(priced)

    alerts = []
    for m in menu:
        if m.food_cost_pct is not None and m.food_cost_pct > 35:
            alerts.append({'type': 'menu', 'name': m.name, 'msg': f'Food cost {m.food_cost_pct:.1f}% — above 35%'})
    for b in batches_:
        if b.is_low_yield:
            alerts.append({'type': 'batch', 'name': b.name, 'msg': f'Low yield {b.yield_percent:.0f}%'})

    return render_template('index.html', menu=menu, batches=batches_,
                           inv_items=inv_items, total_items=total_items,
                           avg_food_cost=avg_food_cost, alerts=alerts, db_url=db_url)

@app.route('/menu', methods=['GET','POST'])
def menu_items():
    if request.method == 'POST':
        name = request.form.get('name','').strip()
        try:
            price = float(request.form.get('price') or 0)
        except Exception:
            price = 0.0
        notes = request.form.get('notes','').strip()
        category = request.form.get('category', 'Other').strip()
        if not name:
            flash('Name is required.')
        elif MenuItem.query.filter_by(name=name).first():
            flash('Menu item already exists.')
        else:
            db.session.add(MenuItem(name=name, price=price, notes=notes, category=category))
            db.session.commit()
            flash(f'"{name}" created.')
        return redirect(url_for('menu_items'))
    menu = MenuItem.query.order_by(MenuItem.category, MenuItem.name).all()
    return render_template('menu.html', menu=menu, db_url=db_url)

@app.route('/menu/<int:menu_id>/clone', methods=['POST'])
def menu_clone(menu_id):
    src = MenuItem.query.get_or_404(menu_id)
    base_name = f"Copy of {src.name}"
    suffix = 0; new_name = base_name
    while MenuItem.query.filter_by(name=new_name).first():
        suffix += 1; new_name = f"{base_name} ({suffix})"
    clone = MenuItem(name=new_name, price=src.price, notes=src.notes, category=src.category)
    db.session.add(clone); db.session.flush()
    for c in src.inv_components:
        db.session.add(MenuIngredient(menu_id=clone.id, item_id=c.item_id, qty=c.qty, unit=c.unit))
    for bp in src.batch_portions:
        db.session.add(MenuBatchPortion(menu_id=clone.id, batch_id=bp.batch_id,
                                        portion_qty=bp.portion_qty, portion_unit=bp.portion_unit))
    db.session.commit()
    flash(f'Cloned as "{new_name}".')
    return redirect(url_for('menu_detail', menu_id=clone.id))

@app.route('/menu/<int:menu_id>', methods=['GET','POST'])
def menu_detail(menu_id):
    m = MenuItem.query.get_or_404(menu_id)
    inv_items = InventoryItem.query.order_by(InventoryItem.name).all()
    batches_ = BatchRecipe.query.order_by(BatchRecipe.name).all()
    action = request.args.get('action','')

    if request.method == 'POST':
        if action == 'add_inv':
            # Bulk add: item_id[], qty[], unit[]
            item_ids = request.form.getlist('item_id[]')
            qtys = request.form.getlist('qty[]')
            units = request.form.getlist('unit[]')
            added = 0
            for item_id, qty, unit in zip(item_ids, qtys, units):
                try:
                    item_id = int(item_id); qty = float(qty); unit = unit.strip().lower()
                except Exception:
                    continue
                if unit not in ('g','kg','oz','lb','ml','l') or qty <= 0:
                    continue
                db.session.add(MenuIngredient(menu_id=m.id, item_id=item_id, qty=qty, unit=unit))
                added += 1
            if added:
                db.session.commit(); flash(f'{added} ingredient(s) added.')
            else:
                flash('No valid ingredients to add.')
        elif action == 'add_batch':
            try:
                batch_id = int(request.form['batch_id'])
                qty = float(request.form['portion_qty'])
                unit = request.form['portion_unit'].strip().lower()
            except Exception:
                flash('Provide batch, portion qty, unit.')
                return redirect(url_for('menu_detail', menu_id=m.id))
            db.session.add(MenuBatchPortion(menu_id=m.id, batch_id=batch_id,
                                            portion_qty=qty, portion_unit=unit))
            db.session.commit(); flash('Batch portion added.')
        elif action == 'update_price':
            m.price = float(request.form.get('price') or 0)
            m.category = request.form.get('category', m.category).strip()
            db.session.commit(); flash('Updated.')
        return redirect(url_for('menu_detail', menu_id=m.id))

    return render_template('menu_detail.html', m=m, inv_items=inv_items,
                           batches=batches_, db_url=db_url)

@app.post('/menu/<int:menu_id>/inv/<int:ing_id>/update')
def menu_update_inv(menu_id, ing_id):
    c = MenuIngredient.query.get_or_404(ing_id)
    try:
        c.qty = float(request.form.get('qty', c.qty) or 0)
    except Exception:
        pass
    unit = (request.form.get('unit') or c.unit).strip().lower()
    if unit in ('g','kg','oz','lb','ml','l'):
        c.unit = unit
    db.session.commit(); flash('Updated.')
    return redirect(url_for('menu_detail', menu_id=menu_id))

@app.post('/menu/<int:menu_id>/batch/<int:bp_id>/update')
def menu_update_batch(menu_id, bp_id):
    bp = MenuBatchPortion.query.get_or_404(bp_id)
    try:
        bp.portion_qty = float(request.form.get('portion_qty', bp.portion_qty) or 0)
    except Exception:
        pass
    unit = (request.form.get('portion_unit') or bp.portion_unit).strip().lower()
    if unit in ('g','kg','oz','lb','ml','l'):
        bp.portion_unit = unit
    db.session.commit(); flash('Updated.')
    return redirect(url_for('menu_detail', menu_id=menu_id))

@app.route('/menu/<int:menu_id>/delete_inv/<int:ing_id>')
def menu_delete_inv(menu_id, ing_id):
    ing = MenuIngredient.query.get_or_404(ing_id)
    db.session.delete(ing); db.session.commit(); flash('Removed.')
    return redirect(url_for('menu_detail', menu_id=menu_id))

@app.route('/menu/<int:menu_id>/delete_batch/<int:bp_id>')
def menu_delete_batch(menu_id, bp_id):
    bp = MenuBatchPortion.query.get_or_404(bp_id)
    db.session.delete(bp); db.session.commit(); flash('Removed.')
    return redirect(url_for('menu_detail', menu_id=menu_id))

@app.route('/menu/<int:menu_id>/delete', methods=['POST'])
def menu_delete(menu_id):
    m = MenuItem.query.get_or_404(menu_id)
    db.session.delete(m); db.session.commit(); flash(f'"{m.name}" deleted.')
    return redirect(url_for('menu_items'))

if __name__ == '__main__':
    app.run(debug=True)
