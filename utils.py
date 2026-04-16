# utils.py
WEIGHT_FACTORS = {'g':1.0,'kg':1000.0,'oz':28.349523125,'lb':453.59237}
VOLUME_FACTORS = {'ml':1.0,'l':1000.0}

def same_dimension(u1, u2):
    u1 = u1.lower(); u2 = u2.lower()
    return (u1 in WEIGHT_FACTORS and u2 in WEIGHT_FACTORS) or (u1 in VOLUME_FACTORS and u2 in VOLUME_FACTORS)

def _to_base(value, unit):
    unit = unit.lower()
    if unit in WEIGHT_FACTORS: return value * WEIGHT_FACTORS[unit]
    if unit in VOLUME_FACTORS: return value * VOLUME_FACTORS[unit]
    raise ValueError(f'Unsupported unit: {unit}')

def convert(value, from_unit, to_unit):
    from_unit = from_unit.lower(); to_unit = to_unit.lower()
    if from_unit == to_unit: return value
    if not same_dimension(from_unit, to_unit): raise ValueError('Unit types do not match')
    base = _to_base(value, from_unit)
    return base / (WEIGHT_FACTORS.get(to_unit) or VOLUME_FACTORS.get(to_unit))

def unit_cost_in(unit_cost, item_unit, target_unit):
    if item_unit.lower() == target_unit.lower(): return unit_cost
    # 1 target_unit expressed in item_unit
    one_target_in_item = convert(1.0, target_unit, item_unit)
    return unit_cost * one_target_in_item
