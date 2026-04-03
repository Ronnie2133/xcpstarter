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

# ---------- DB ----------
db_url = os.environ.get('DATABASE_URL', '')

if db_url.startswith('postgres://'):
    db_url = db_url.replace('postgres://', 'postgresql+psycopg://', 1)
elif db_url.startswith('postgresql://'):
    db_url = db_url.replace('postgresql://', 'postgresql+psycopg://', 1)

if not db_url:
    db_url = 'sqlite:///app.db'

app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# ---------- MODELS ----------

class InventoryItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    unit = db.Column(db.String(10), nullable=False)
    unit_cost = db.Column(db.Float, default=0.0)


class BatchRecipe(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), unique=True, nullable=False)

    raw_qty = db.Column(db.Float, nullable=True)  # NEW

    yield_qty = db.Column(db.Float, nullable=False)
    yield_unit = db.Column(db.String(10), nullable=False)
    notes = db.Column(db.Text, default='')

    ingredients = relationship('BatchIngredient', backref='batch', cascade='all, delete-orphan')

    @property
    def total_cost(self):
        return sum(i.ext_cost for i in self.ingredients)

    @property
    def cost_per_yield_unit(self):
        if not self.yield_qty:
            return 0
        return self.total_cost / self.yield_qty

    @property
    def yield_percent(self):
        if not self.raw_qty:
            return None
        return (self.yield_qty / self.raw_qty) * 100


class BatchIngredient(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    batch_id = db.Column(db.Integer, db.ForeignKey('batch_recipe.id'))
    item_id = db.Column(db.Integer, db.ForeignKey('inventory_item.id'))
    qty = db.Column(db.Float)
    unit = db.Column(db.String(10))
    item = relationship('InventoryItem')

    @property
    def ext_cost(self):
        return unit_cost_in(self.item.unit_cost, self.item.unit, self.unit) * self.qty


class MenuItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), unique=True)
    price = db.Column(db.Float)
    components = relationship('MenuBatchPortion', backref='menu')

    @property
    def cost(self):
        return sum(c.ext_cost for c in self.components)


class MenuBatchPortion(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    menu_id = db.Column(db.Integer, db.ForeignKey('menu_item.id'))
    batch_id = db.Column(db.Integer, db.ForeignKey('batch_recipe.id'))
    qty = db.Column(db.Float)
    unit = db.Column(db.String(10))
    batch = relationship('BatchRecipe')

    @property
    def ext_cost(self):
        if not self.batch.yield_qty:
            return 0
        qty_converted = convert(self.qty, self.unit, self.batch.yield_unit)
        return (qty_converted / self.batch.yield_qty) * self.batch.total_cost


# ---------- ROUTES ----------

@app.before_request
def setup():
    db.create_all()


@app.route('/')
def index():
    return render_template('index.html')


# ---------- INVENTORY ----------

@app.route('/inventory', methods=['GET','POST'])
def inventory():
    if request.method == 'POST':
        db.session.add(InventoryItem(
            name=request.form['name'],
            unit=request.form['unit'],
            unit_cost=float(request.form['unit_cost'])
        ))
        db.session.commit()
        return redirect(url_for('inventory'))

    items = InventoryItem.query.all()
    return render_template('inventory.html', items=items)


# ---------- BATCHES ----------

@app.route('/batches', methods=['GET','POST'])
def batches():
    if request.method == 'POST':
        db.session.add(BatchRecipe(
            name=request.form['name'],
            raw_qty=float(request.form.get('raw_qty') or 0),
            yield_qty=float(request.form['yield_qty']),
            yield_unit=request.form['yield_unit'],
            notes=request.form.get('notes','')
        ))
        db.session.commit()
        return redirect(url_for('batches'))

    return render_template('batches.html', batches=BatchRecipe.query.all())


@app.route('/batches/<int:id>', methods=['GET','POST'])
def batch_detail(id):
    b = BatchRecipe.query.get_or_404(id)

    if request.method == 'POST':
        db.session.add(BatchIngredient(
            batch_id=id,
            item_id=int(request.form['item_id']),
            qty=float(request.form['qty']),
            unit=request.form['unit']
        ))
        db.session.commit()

    items = InventoryItem.query.all()
    return render_template('batch_detail.html', b=b, items=items)


# ---------- MENU ----------

@app.route('/menu', methods=['GET','POST'])
def menu():
    if request.method == 'POST':
        db.session.add(MenuItem(
            name=request.form['name'],
            price=float(request.form['price'])
        ))
        db.session.commit()
        return redirect(url_for('menu'))

    return render_template('menu.html', menu=MenuItem.query.all())


@app.route('/menu/<int:id>', methods=['GET','POST'])
def menu_detail(id):
    m = MenuItem.query.get_or_404(id)

    if request.method == 'POST':
        db.session.add(MenuBatchPortion(
            menu_id=id,
            batch_id=int(request.form['batch_id']),
            qty=float(request.form['qty']),
            unit=request.form['unit']
        ))
        db.session.commit()

    batches = BatchRecipe.query.all()
    return render_template('menu_detail.html', m=m, batches=batches)


# ---------- RUN ----------

if __name__ == '__main__':
    app.run(debug=True)
