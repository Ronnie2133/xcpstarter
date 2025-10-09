from __future__ import annotations
import os, io, csv
from flask import (
    Flask, render_template, request, redirect, url_for, flash, session, Response
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm import relationship
from dotenv import load_dotenv
from utils import convert, same_dimension, unit_cost_in

load_dotenv()
app = Flask(__name__, instance_relative_config=True)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret')

# --- Database config (Render-safe) ---
db_url = os.environ.get('DATABASE_URL', '')

# --- Fix Render/Heroku URLs and enable psycopg v3 ---
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

# === Sub-batches: allow batches to include other batches ===
class BatchRecipe(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), unique=True, nullable=False)
    yield_qty = db.Column(db.Float, nullable=False)
    yield_unit = db.Column(db.String(10), nullable=False)
    notes = db.Column(db.Text, default='')
    ingredients = relationship('BatchIngredient', backref='batch', cascade='all, delete-orphan')

    @property
    def total_cost(self) -> float:
        # inventory ingredient cost
        inv_cost = sum(ing.ext_cost for ing in self.ingredients)
        # sub-batch cost (defensive: don't crash UI over unit mistakes)
        sub_cost = 0.0
        for sb in getattr(self, 'sub_batches', []):
            try:
                sub_cost += sb.ext_cost
            except Exception:
                # bad unit or zero yield on child; treat as $0 so page still renders
                continue
        return inv_cost + sub_cost

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
        # Safe cost math: return 0 if dimensions mismatch or invalid child yield
        if not self.child or (self.child.yield_qty or 0) <= 0:
            return 0.0
        if not same_dimension(self.unit, self.child.yield_unit):
            return 0.0
        portion_in_yield_unit = convert(self.qty, self.unit, self.child.yield_unit)
        return (portion_in_yield_unit / self.child.yield_qty) * (self.child.total_cost or 0.0)
# === /Sub-batches ===

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
            return 0.0
        if (self.batch.yield_qty or 0) <= 0:
            return 0.0
        portion_in_yield_unit = convert(self.portion_qty, self.portion_unit, self.batch.yield_unit)
        return (portion_in_yield_unit / self.batch.yield_qty) * (self.batch.total_cost or 0.0)

# ---------- Auth via env ----------
STAFF_USER = os.environ.get('STAFF_USERNAME', None)
STAFF_PASS = os.environ.get('STAFF_PASSWORD', None)

@app.before_request
def _ensure_tables_and_auth():
    # Ensure tables exist once per process
    if not getattr(app, "_tables_created", False):
        with app.app_context():
            db.create_all()
        app._tables_created = True
        maybe_seed()

    # Allow login/static without auth
    if request.endpoint in ('login', 'static'):
        return

    # If STAFF env vars are set, require login
    if STAFF_USER and STAFF_PASS:
        if not session.get('auth_ok'):
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
    if InventoryItem.query.first():
        return
    db.session.add_all([
        InventoryItem(name='Rice', unit='g', unit_cost=0.003),
        InventoryItem(name='Chicken', unit='lb', unit_cost=2.50),
        InventoryItem(name='Olive Oil', unit='ml', unit_cost=0.004),
    ])
    db.session.commit()

# ---------- Routes ----------
@app.route('/')
def index():
    menu = MenuItem.query.all()
    return render_template('index.html', menu=menu, db_url=db_url)

# ----- Inventory -----
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

# ----- Batches -----
@app.route('/batches', methods=['GET', 'POST'])
def batches():
    if request.method == 'POST':
        name = request.form.get('name','').strip()
        try:
            yield_qty = float(request.form.get('yield_qty') or 0)
        except Exception:
            yield_qty = 0.0
        yield_unit = (request.form.get('yield_unit') or '').strip().lower()
        notes = (request.form.get('notes') or '').strip()

        if not name or yield_unit not in ('g','kg','oz','lb','ml','l'):
            flash('Name and a valid yield unit are required.')
        elif BatchRecipe.query.filter_by(name=name).first():
            flash('Batch name already exists.')
        else:
            db.session.add(BatchRecipe(name=name, yield_qty=yield_qty, yield_unit=yield_unit, notes=notes))
            db.session.commit()
            flash('Batch created.')
        return redirect(url_for('batches'))

    batches_ = BatchRecipe.query.order_by(BatchRecipe.name).all()
    return render_template('batches.html', batches=batches_, db_url=db_url)

@app.route('/batches/<int:batch_id>', methods=['GET', 'POST'])
def batch_detail(batch_id):
    b = BatchRecipe.query.get_or_404(batch_id)
    items = InventoryItem.query.order_by(InventoryItem.name).all()
    if request.method == 'POST':
        try:
            item_id = int(request.form['item_id'])
            qty = float(request.form['qty'])
            unit = request.form['unit'].strip().lower()
        except Exception:
            flash('Please provide item, qty, and unit.'); 
            return redirect(url_for('batch_detail', batch_id=b.id))

        if unit not in ('g','kg','oz','lb','ml','l'):
            flash('Unit must be one of g, kg, oz, lb, ml, l.')
        else:
            db.session.add(BatchIngredient(batch_id=b.id, item_id=item_id, qty=qty, unit=unit))
            db.session.commit()
            flash('Ingredient added.')
        return redirect(url_for('batch_detail', batch_id=b.id))

    all_batches = BatchRecipe.query.order_by(BatchRecipe.name).all()
    return render_template('batch_detail.html', b=b, items=items, all_batches=all_batches, db_url=db_url)

@app.route('/batches/<int:batch_id>/delete_ing/<int:ing_id>')
def batch_delete_ing(batch_id, ing_id):
    ing = BatchIngredient.query.get_or_404(ing_id)
    db.session.delete(ing); db.session.commit()
    flash('Removed ingredient.')
    return redirect(url_for('batch_detail', batch_id=batch_id))

# CSV export/import for batches (with nested sub-batches)
@app.route('/batches/export.csv')
def batches_export_csv():
    si = io.StringIO(); w = csv.writer(si)
    w.writerow(['batch_name','yield_qty','yield_unit','notes','component_type','component_name','qty','unit'])
    for b in BatchRecipe.query.order_by(BatchRecipe.name).all():
        # inventory components
        for ing in b.ingredients:
            w.writerow([b.name, b.yield_qty, b.yield_unit, b.notes or '',
                        'inventory', ing.item.name, ing.qty, ing.unit])
        # sub-batches
        for sb in getattr(b, 'sub_batches', []):
            w.writerow([b.name, b.yield_qty, b.yield_unit, b.notes or '',
                        'batch', sb.child.name, sb.qty, sb.unit])
    return Response(si.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment; filename=batches.csv'})

@app.route('/batches/import.csv', methods=['POST'])
def batches_import_csv():
    f = request.files.get('file')
    if not f or not f.filename.endswith('.csv'):
        flash('Please upload a .csv file.'); return redirect(url_for('batches'))

    text = f.stream.read().decode('utf-8', errors='replace')
    rows = list(csv.DictReader(io.StringIO(text)))

    from collections import defaultdict
    groups = defaultdict(list)
    for r in rows:
        name = (r.get('batch_name') or '').strip()
        if name: groups[name].append(r)

    created = 0; updated = 0; skipped = 0
    for bname, items in groups.items():
        first = items[0]
        try:
            yield_qty  = float(first.get('yield_qty') or 0)
        except Exception:
            yield_qty = 0
        yield_unit = (first.get('yield_unit') or '').strip().lower()
        notes      = (first.get('notes') or '').strip()

        if yield_qty <= 0 or yield_unit not in ('g','kg','oz','lb','ml','l'):
            skipped += 1; continue

        batch = BatchRecipe.query.filter_by(name=bname).first()
        if not batch:
            batch = BatchRecipe(name=bname, yield_qty=yield_qty, yield_unit=yield_unit, notes=notes)
            db.session.add(batch); db.session.flush()
            created += 1
        else:
            batch.yield_qty = yield_qty; batch.yield_unit = yield_unit; batch.notes = notes
            # clear existing comps to avoid duplicates
            BatchIngredient.query.filter_by(batch_id=batch.id).delete()
            BatchSubBatch.query.filter_by(parent_id=batch.id).delete()
            updated += 1

        for r in items:
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
                    # Placeholder child batch so import order doesn’t matter
                    child = BatchRecipe(name=cname, yield_qty=1.0, yield_unit=unit, notes='(placeholder)')
                    db.session.add(child); db.session.flush()
                db.session.add(BatchSubBatch(parent_id=batch.id, child_id=child.id, qty=qty, unit=unit))
            else:
                continue

    db.session.commit()
    flash(f'Batches import complete. Created {created}, updated {updated}, skipped {skipped}.')
    return redirect(url_for('batches'))

# Sub-batch UI helpers (used by batch_detail.html)
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
    db.session.delete(sb); db.session.commit()
    flash('Removed sub-batch.')
    return redirect(url_for('batch_detail', batch_id=batch_id))

# ----- Menu -----
@app.route('/menu', methods=['GET','POST'])
def menu_items():
    if request.method == 'POST':
        name = request.form.get('name','').strip()
        try:
            price = float(request.form.get('price') or 0)
        except Exception:
            price = 0.0
        notes = request.form.get('notes','').strip()
        if not name:
            flash('Name is required.')
        elif MenuItem.query.filter_by(name=name).first():
            flash('Menu item already exists.')
        else:
            db.session.add(MenuItem(name=name, price=price, notes=notes))
            db.session.commit()
            flash('Menu item created.')
        return redirect(url_for('menu_items'))
    menu = MenuItem.query.order_by(MenuItem.name).all()
    return render_template('menu.html', menu=menu, db_url=db_url)

@app.route('/menu/<int:menu_id>', methods=['GET','POST'])
def menu_detail(menu_id):
    m = MenuItem.query.get_or_404(menu_id)
    inv_items = InventoryItem.query.order_by(InventoryItem.name).all()
    batches_ = BatchRecipe.query.order_by(BatchRecipe.name).all()
    action = request.args.get('action','')

    if request.method == 'POST':
        if action == 'add_inv':
            try:
                item_id = int(request.form['item_id'])
                qty = float(request.form['qty'])
                unit = request.form['unit'].strip().lower()
            except Exception:
                flash('Provide item, qty, unit.')
                return redirect(url_for('menu_detail', menu_id=m.id))
            if unit not in ('g','kg','oz','lb','ml','l'):
                flash('Unit must be one of g, kg, oz, lb, ml, l.')
            else:
                db.session.add(MenuIngredient(menu_id=m.id, item_id=item_id, qty=qty, unit=unit))
                db.session.commit(); flash('Inventory component added.')
        elif action == 'add_batch':
            try:
                batch_id = int(request.form['batch_id'])
                qty = float(request.form['portion_qty'])
                unit = request.form['portion_unit'].strip().lower()
            except Exception:
                flash('Provide batch, portion qty, unit.')
                return redirect(url_for('menu_detail', menu_id=m.id))
            db.session.add(MenuBatchPortion(menu_id=m.id, batch_id=batch_id, portion_qty=qty, portion_unit=unit))
            db.session.commit(); flash('Batch portion added.')
        elif action == 'update_price':
            m.price = float(request.form.get('price') or 0)
            db.session.commit(); flash('Price updated.')
        return redirect(url_for('menu_detail', menu_id=m.id))

    return render_template('menu_detail.html', m=m, inv_items=inv_items, batches=batches_, db_url=db_url)

@app.route('/menu/<int:menu_id>/delete_inv/<int:ing_id>')
def menu_delete_inv(menu_id, ing_id):
    ing = MenuIngredient.query.get_or_404(ing_id)
    db.session.delete(ing); db.session.commit()
    flash('Removed inventory component.')
    return redirect(url_for('menu_detail', menu_id=menu_id))

@app.route('/menu/<int:menu_id>/delete_batch/<int:bp_id>')
def menu_delete_batch(menu_id, bp_id):
    bp = MenuBatchPortion.query.get_or_404(bp_id)
    db.session.delete(bp); db.session.commit()
    flash('Removed batch portion.')
    return redirect(url_for('menu_detail', menu_id=menu_id))

# ---------- Run ----------
if __name__ == '__main__':
    app.run(debug=True)
