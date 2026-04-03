# --- ONLY SHOWING CHANGES YOU NEED TO INSERT ---

# =========================
# 1. MODIFY BatchRecipe MODEL
# =========================

class BatchRecipe(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), unique=True, nullable=False)

    raw_qty = db.Column(db.Float, nullable=True)  # ✅ NEW

    yield_qty = db.Column(db.Float, nullable=False)
    yield_unit = db.Column(db.String(10), nullable=False)
    notes = db.Column(db.Text, default='')

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

    # ✅ NEW
    @property
    def yield_percent(self):
        if not self.raw_qty or self.raw_qty == 0:
            return None
        return (self.yield_qty / self.raw_qty) * 100

    # ✅ NEW
    @property
    def cost_per_yield_unit(self):
        if not self.yield_qty or self.yield_qty == 0:
            return 0
        return self.total_cost / self.yield_qty

    # ✅ NEW
    @property
    def is_low_yield(self):
        yp = self.yield_percent
        if yp is None:
            return False
        return yp < 70


# =========================
# 2. UPDATE CREATE BATCH
# =========================

@app.route('/batches', methods=['GET', 'POST'])
def batches():
    if request.method == 'POST':
        name = request.form.get('name','').strip()

        try:
            yield_qty = float(request.form.get('yield_qty') or 0)
        except Exception:
            yield_qty = 0.0

        # ✅ NEW
        try:
            raw_qty = float(request.form.get('raw_qty') or 0)
        except Exception:
            raw_qty = None

        yield_unit = (request.form.get('yield_unit') or '').strip().lower()
        notes = (request.form.get('notes') or '').strip()

        if not name or yield_unit not in ('g','kg','oz','lb','ml','l'):
            flash('Name and a valid yield unit are required.')
        elif BatchRecipe.query.filter_by(name=name).first():
            flash('Batch name already exists.')
        else:
            db.session.add(BatchRecipe(
                name=name,
                yield_qty=yield_qty,
                yield_unit=yield_unit,
                notes=notes,
                raw_qty=raw_qty  # ✅ NEW
            ))
            db.session.commit()
            flash('Batch created.')

        return redirect(url_for('batches'))

    batches_ = BatchRecipe.query.order_by(BatchRecipe.name).all()
    return render_template('batches.html', batches=batches_, db_url=db_url)


# =========================
# 3. UPDATE EDIT BATCH
# =========================

@app.route('/batches/<int:batch_id>/update', methods=['POST'])
def batch_update(batch_id):
    b = BatchRecipe.query.get_or_404(batch_id)
    allowed = {'g','kg','oz','lb','ml','l'}

    name = (request.form.get('name') or b.name).strip()
    notes = (request.form.get('notes') or b.notes).strip()

    try:
        yield_qty = float(request.form.get('yield_qty') or b.yield_qty)
    except Exception:
        yield_qty = b.yield_qty

    # ✅ NEW
    try:
        raw_qty = float(request.form.get('raw_qty') or b.raw_qty)
    except Exception:
        raw_qty = b.raw_qty

    yield_unit = (request.form.get('yield_unit') or b.yield_unit).strip().lower()

    if not name or yield_qty <= 0 or yield_unit not in allowed:
        flash('Please provide a valid name, positive yield, and a valid unit.')
        return redirect(url_for('batch_detail', batch_id=b.id))

    exists = BatchRecipe.query.filter(BatchRecipe.name==name, BatchRecipe.id!=b.id).first()
    if exists:
        flash('Another batch already uses that name.')
        return redirect(url_for('batch_detail', batch_id=b.id))

    b.name = name
    b.yield_qty = yield_qty
    b.yield_unit = yield_unit
    b.notes = notes
    b.raw_qty = raw_qty  # ✅ NEW

    db.session.commit()
    flash('Batch updated.')
    return redirect(url_for('batch_detail', batch_id=b.id))
