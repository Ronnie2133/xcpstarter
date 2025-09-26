WEIGHT_FACTORS={'g':1.0,'kg':1000.0,'oz':28.349523125,'lb':453.59237}
VOLUME_FACTORS={'ml':1.0,'l':1000.0}

def same_dimension(u1,u2):
    u1=u1.lower(); u2=u2.lower()
    return (u1 in WEIGHT_FACTORS and u2 in WEIGHT_FACTORS) or (u1 in VOLUME_FACTORS and u2 in VOLUME_FACTORS)

def to_base(v,u):
    u=u.lower()
    if u in WEIGHT_FACTORS: return v*WEIGHT_FACTORS[u]
    if u in VOLUME_FACTORS: return v*VOLUME_FACTORS[u]
    raise ValueError('Unsupported unit')

def convert(v,f,t):
    f=f.lower(); t=t.lower()
    if not same_dimension(f,t): raise ValueError('Cannot convert across weight/volume without density.')
    base=to_base(v,f)
    if t in WEIGHT_FACTORS: return base/WEIGHT_FACTORS[t]
    if t in VOLUME_FACTORS: return base/VOLUME_FACTORS[t]
    raise ValueError('Unsupported unit')

def unit_cost_in(cost, item_unit, target_unit):
    per_base = cost / to_base(1.0, item_unit)
    return per_base * to_base(1.0, target_unit)
