"""
IRON RALLY - player car builder (Blender 5.x, run headless)

  blender -b --factory-startup -P tools/build_car.py -- <out_dir> [--preview <png_dir>]

Builds an R35-style 2-door grand tourer procedurally:
  * body = lofted cross-sections -> subdivision surface -> boolean recesses
    (wheel wells, grille, intakes, headlight sockets, tail-light pods, diffuser, fender vents)
  * glass / trim assigned by region, sharp edges marked for crisp boolean cuts
  * details joined into the body (headlights, 4 ring tail lights, quad exhaust, mirrors ...)
  * ambient occlusion baked into vertex colours with Cycles (exported as COLOR_0)
  * variant parts (Wing_Std / Wing_GT / Aero_GT / BullBar / LightBar) and the Wheel / Caliper
    are separate nodes so the game can toggle / instance them.
Coordinates: L = length (front +), W = width, H = height. Blender (x, y, z) = (W, -L, H),
which the glTF exporter turns into three.js (x=W, y=H, z=L)  -> car faces +Z in the game.
"""
import bpy, bmesh, math, os, sys
from mathutils import Vector, Matrix

args = sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else []
OUT_DIR = args[0] if args else os.getcwd()
PREVIEW_DIR = args[args.index('--preview') + 1] if '--preview' in args else None

bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene


def V(L, W, H):
    return Vector((W, -L, H))


# ------------------------------------------------------------------ materials
MATS = {}


def mat(name, color, metal=0.0, rough=0.5, emit=None):
    m = bpy.data.materials.new(name)
    try:
        m.use_nodes = True
    except Exception:
        pass
    bsdf = m.node_tree.nodes.get('Principled BSDF') if m.node_tree else None
    if bsdf:
        bsdf.inputs['Base Color'].default_value = (*color, 1)
        bsdf.inputs['Metallic'].default_value = metal
        bsdf.inputs['Roughness'].default_value = rough
        if emit and 'Emission Color' in bsdf.inputs:
            bsdf.inputs['Emission Color'].default_value = (*emit, 1)
            bsdf.inputs['Emission Strength'].default_value = 4.0
    m.diffuse_color = (*color, 1)
    m.metallic = metal
    m.roughness = rough
    MATS[name] = m
    return m


mat('Paint', (0.55, 0.02, 0.03), 0.6, 0.3)
mat('Glass', (0.02, 0.03, 0.04), 0.2, 0.05)
mat('Trim', (0.02, 0.02, 0.022), 0.1, 0.45)
mat('Chrome', (0.9, 0.9, 0.9), 1.0, 0.08)
mat('Carbon', (0.03, 0.03, 0.035), 0.3, 0.3)
mat('WellLiner', (0.012, 0.012, 0.012), 0.0, 0.9)
mat('Grille', (0.01, 0.01, 0.012), 0.2, 0.6)
mat('HeadLens', (0.6, 0.65, 0.7), 0.0, 0.02)
mat('Reflector', (0.85, 0.85, 0.88), 1.0, 0.12)
mat('HeadLED', (1.0, 1.0, 1.0), 0.0, 0.3, emit=(1.0, 0.97, 0.9))
mat('TailLED', (0.8, 0.02, 0.02), 0.0, 0.3, emit=(1.0, 0.05, 0.03))
mat('TailLens', (0.25, 0.01, 0.01), 0.0, 0.05)
mat('Exhaust', (0.55, 0.5, 0.45), 1.0, 0.25)
mat('Tire', (0.02, 0.02, 0.02), 0.0, 0.85)
mat('Rim', (0.12, 0.12, 0.13), 1.0, 0.3)
mat('Brake', (0.45, 0.45, 0.46), 1.0, 0.35)
mat('Caliper', (0.75, 0.05, 0.03), 0.2, 0.4)
mat('Amber', (1.0, 0.55, 0.05), 0.0, 0.3, emit=(1.0, 0.5, 0.0))
mat('Underbody', (0.06, 0.065, 0.07), 0.7, 0.55)
BODY_MATS = ['Paint', 'Glass', 'Trim', 'WellLiner', 'Grille', 'Carbon', 'Chrome', 'HeadLens', 'Reflector', 'HeadLED', 'TailLED', 'TailLens', 'Exhaust', 'Underbody']


def new_obj(name, bm, materials):
    me = bpy.data.meshes.new(name)
    bm.to_mesh(me)
    bm.free()
    ob = bpy.data.objects.new(name, me)
    scene.collection.objects.link(ob)
    for m in materials:
        me.materials.append(MATS[m])
    return ob


def apply_modifiers(ob):
    dg = bpy.context.evaluated_depsgraph_get()
    eo = ob.evaluated_get(dg)
    me = bpy.data.meshes.new_from_object(eo)
    ob.modifiers.clear()
    old = ob.data
    ob.data = me
    bpy.data.meshes.remove(old)


def smooth_all(ob, sharp_deg=None):
    me = ob.data
    for p in me.polygons:
        p.use_smooth = True
    if sharp_deg is not None:
        bm = bmesh.new()
        bm.from_mesh(me)
        lim = math.radians(sharp_deg)
        for e in bm.edges:
            if len(e.link_faces) == 2 and e.calc_face_angle(0) > lim:
                e.smooth = False
        bm.to_mesh(me)
        bm.free()


def transform_bm(bm, mtx):
    bmesh.ops.transform(bm, matrix=mtx, verts=bm.verts)


def box_bm(sx, sy, sz, mtx=Matrix(), mat_index=0):
    """box sized in Blender axes (x=W, y=L, z=H)"""
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0)
    transform_bm(bm, Matrix.Diagonal((sx, sy, sz, 1)))
    transform_bm(bm, mtx)
    for f in bm.faces:
        f.material_index = mat_index
    return bm


def cyl_bm(r, depth, segs=24, mtx=Matrix(), mat_index=0, r2=None):
    bm = bmesh.new()
    bmesh.ops.create_cone(bm, cap_ends=True, cap_tris=False, segments=segs, radius1=r, radius2=r if r2 is None else r2, depth=depth)
    transform_bm(bm, mtx)
    for f in bm.faces:
        f.material_index = mat_index
    return bm


def merge_bm(dst, src):
    """append src bmesh into dst (keeps material indices)"""
    me = bpy.data.meshes.new('tmp')
    src.to_mesh(me)
    src.free()
    dst.from_mesh(me)
    bpy.data.meshes.remove(me)


def M(L=0, W=0, H=0, rx=0, ry=0, rz=0):
    """placement matrix: translation in L/W/H + rotation (degrees) about blender x/y/z"""
    return Matrix.Translation(V(L, W, H)) @ Matrix.Rotation(math.radians(rz), 4, 'Z') @ Matrix.Rotation(math.radians(ry), 4, 'Y') @ Matrix.Rotation(math.radians(rx), 4, 'X')


# ------------------------------------------------------------------ body loft
# (L, zb, zs, zg, zt, wb, ws, wg, wt, crown, fender_peak)
KEYS = [
    (-2.38, .36, .60, .74, .80, .60, .66, .58, .44, .000, .00),
    (-2.33, .30, .68, .84, .88, .76, .84, .76, .58, .010, .00),
    (-2.20, .25, .76, .91, .94, .84, .92, .84, .68, .015, .00),
    (-1.95, .22, .80, .93, .965, .86, .95, .86, .70, .015, .00),
    (-1.70, .22, .82, .94, 1.00, .86, .962, .85, .66, .010, .00),
    (-1.39, .22, .84, .95, 1.13, .86, .975, .84, .62, .008, .00),
    (-1.05, .22, .83, .96, 1.26, .86, .962, .83, .59, .008, .00),
    (-0.70, .22, .82, .96, 1.34, .86, .952, .83, .575, .010, .00),
    (-0.25, .22, .81, .955, 1.37, .86, .947, .835, .575, .010, .00),
    (0.15, .22, .80, .95, 1.345, .86, .947, .835, .58, .010, .00),
    (0.50, .22, .79, .94, 1.19, .86, .950, .84, .62, .008, .00),
    (0.80, .22, .785, .925, 1.01, .86, .955, .85, .68, .010, .01),
    (1.00, .22, .78, .885, .925, .86, .962, .88, .74, .020, .030),
    (1.39, .23, .77, .845, .865, .86, .975, .89, .74, .025, .035),
    (1.80, .24, .73, .80, .815, .85, .955, .86, .72, .020, .030),
    (2.10, .25, .67, .745, .76, .83, .915, .81, .65, .012, .022),
    (2.28, .28, .59, .68, .70, .78, .850, .74, .56, .004, .012),
    (2.38, .33, .52, .61, .64, .66, .730, .62, .45, .000, .000),
]


def interp(L):
    ks = KEYS
    if L <= ks[0][0]:
        return list(ks[0][1:])
    if L >= ks[-1][0]:
        return list(ks[-1][1:])
    i = 0
    for i in range(len(ks) - 1):
        if ks[i][0] <= L <= ks[i + 1][0]:
            break
    p0, p1, p2, p3 = ks[max(i - 1, 0)], ks[i], ks[i + 1], ks[min(i + 2, len(ks) - 1)]
    h = p2[0] - p1[0]
    t = (L - p1[0]) / h
    t2, t3 = t * t, t * t * t
    h00, h10, h01, h11 = 2 * t3 - 3 * t2 + 1, t3 - 2 * t2 + t, -2 * t3 + 3 * t2, t3 - t2
    out = []
    for j in range(1, len(p1)):
        m1 = (p2[j] - p0[j]) / (p2[0] - p0[0]) * h if p2[0] != p0[0] else 0
        m2 = (p3[j] - p1[j]) / (p3[0] - p1[0]) * h if p3[0] != p1[0] else 0
        out.append(h00 * p1[j] + h10 * m1 + h01 * p2[j] + h11 * m2)
    return out


def section(L):
    zb, zs, zg, zt, wb, ws, wg, wt, crown, fpk = interp(L)
    return [
        (0.0, zb),
        (wb * 0.75, zb),
        (wb, zb + 0.03),
        (ws * 0.99, zb + (zs - zb) * 0.45),
        (ws, zs),
        (ws - 0.025, zs + 0.045),
        (wg, zg),
        (wg + (wt - wg) * 0.55, zg + (zt - zg) * 0.6),
        (wt, zt - 0.01 + fpk),
        (wt * 0.55, zt + crown * 0.6),
        (0.0, zt + crown),
    ]


def build_body():
    Ls = [-2.38, -2.37, -2.35, -2.32, -2.28]
    x = -2.2
    while x < 2.2:
        Ls.append(round(x, 3))
        x += 0.1
    Ls += [2.24, 2.29, 2.33, 2.36, 2.38]
    bm = bmesh.new()
    rings = []
    for L in Ls:
        half = section(L)
        ring = [half[0]] + half[1:-1] + [half[-1]] + [(-w, z) for (w, z) in reversed(half[1:-1])]
        rings.append([bm.verts.new(V(L, w, z)) for (w, z) in ring])
    n = len(rings[0])
    for i, (a, b) in enumerate(zip(rings[:-1], rings[1:])):
        L0, L1 = Ls[i], Ls[i + 1]
        for k in range(n):
            f = bm.faces.new((a[k], a[(k + 1) % n], b[(k + 1) % n], b[k]))
            # windshield / rear glass follow the base edge loops so their outline stays smooth after subdivision
            if k in (8, 9, 10, 11) and ((0.30 <= L0 and L1 <= 0.905) or (-1.705 <= L0 and L1 <= -0.895)):
                f.material_index = 1
    bm.faces.new(rings[0])
    bm.faces.new(list(reversed(rings[-1])))
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    ob = new_obj('Body', bm, BODY_MATS)
    sub = ob.modifiers.new('sub', 'SUBSURF')
    sub.levels = 2
    sub.render_levels = 2
    apply_modifiers(ob)
    return ob


def point_in_poly(x, y, poly):
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi):
            inside = not inside
        j = i
    return inside


SIDE_WINDOW = [(0.80, 0.985), (0.55, 1.12), (0.32, 1.255), (-0.25, 1.315), (-0.80, 1.262), (-1.30, 1.05), (-1.20, 1.0), (-0.6, 0.99)]
MI = {name: i for i, name in enumerate(BODY_MATS)}


def assign_regions(ob):
    me = ob.data
    for p in me.polygons:
        if p.material_index != MI['Paint']:
            continue
        c, nrm = p.center, p.normal
        L, H = -c.y, c.z
        if H < 0.265 or (L > 2.2 and H < 0.36 and nrm.z < 0.2):
            p.material_index = MI['Trim']                                 # lower lip / rocker


# side windows: front door glass + rear quarter with the characteristic kink (L, H outlines)
WIN_FRONT = [(0.74, 0.99), (0.52, 1.10), (0.28, 1.22), (-0.17, 1.275), (-0.17, 0.985)]
WIN_REAR = [(-0.29, 1.295), (-0.80, 1.252), (-1.29, 1.05), (-1.17, 0.995), (-0.29, 0.985)]


def window_cutter_bm(poly, s):
    bm = bmesh.new()
    a = [bm.verts.new(V(L, s * 0.55, H)) for (L, H) in poly]
    b = [bm.verts.new(V(L, s * 1.3, H)) for (L, H) in poly]
    n = len(poly)
    bm.faces.new(a)
    bm.faces.new(list(reversed(b)))
    for k in range(n):
        bm.faces.new((a[k], a[(k + 1) % n], b[(k + 1) % n], b[k]))
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    for f in bm.faces:
        f.material_index = MI['Trim']
    return bm


def surface_w(L, z):
    """half-width of the greenhouse control surface at (L, z), for fitting the glass panels"""
    sec = section(L)[6:9]
    for (w0, z0), (w1, z1) in zip(sec[:-1], sec[1:]):
        if z0 <= z <= z1:
            return w0 + (w1 - w0) * (z - z0) / max(z1 - z0, 1e-6)
    return sec[0][0] if z < sec[0][1] else sec[-1][0]


def side_glass_bm():
    bm = bmesh.new()
    Ls = [-1.36 + 0.05 * i for i in range(46)]
    zs = [0.95 + 0.02 * j for j in range(20)]
    for s in (1, -1):
        grid = {}
        for i, L in enumerate(Ls):
            zg, zt = interp(L)[2], interp(L)[3]
            for j, z in enumerate(zs):
                near_window = any(point_in_poly(L + dl, z + dz, poly) for poly in (WIN_FRONT, WIN_REAR)
                                  for dl in (-0.05, 0, 0.05) for dz in (-0.04, 0, 0.04))
                if zg - 0.02 <= z <= zt - 0.055 and near_window:
                    grid[(i, j)] = bm.verts.new(V(L, s * (surface_w(L, z) - 0.05), z))
        for (i, j), v in list(grid.items()):
            q = [grid.get(k) for k in ((i, j), (i + 1, j), (i + 1, j + 1), (i, j + 1))]
            if all(q):
                f = bm.faces.new(q if s > 0 else list(reversed(q)))
                f.material_index = MI['Glass']
    return bm


# ------------------------------------------------------------------ boolean cutters
HL_SIZE = (0.36, 0.44, 0.11)


def HL(s):
    """headlight socket placement (sweeps up and back from the grille corner along the fender)"""
    return M(2.16, s * 0.62, 0.69, rz=s * -28, ry=s * -7, rx=-7)


def build_cutters():
    bm = bmesh.new()
    # wheel wells (one per corner, from W=0.45 outwards so the middle of the car stays solid)
    for L in (1.39, -1.39):
        for s in (1, -1):
            merge_bm(bm, cyl_bm(0.43, 0.75, 48, M(L, s * 0.85, 0.36, ry=90), MI['WellLiner']))
    # lower front grille (trapezoid prism)
    g = bmesh.new()
    top, bot, z0, z1 = 0.42, 0.33, 0.30, 0.53
    vs = [g.verts.new(V(2.12, sx * w, z)) for (sx, w, z) in ((-1, bot, z0), (1, bot, z0), (1, top, z1), (-1, top, z1))]
    ve = [g.verts.new(V(2.7, sx * w, z)) for (sx, w, z) in ((-1, bot, z0), (1, bot, z0), (1, top, z1), (-1, top, z1))]
    g.faces.new(vs)
    g.faces.new(list(reversed(ve)))
    for k in range(4):
        g.faces.new((vs[k], ve[k], ve[(k + 1) % 4], vs[(k + 1) % 4]))
    bmesh.ops.recalc_face_normals(g, faces=g.faces)
    for f in g.faces:
        f.material_index = MI['Grille']
    merge_bm(bm, g)
    # upper slot under the hood edge
    merge_bm(bm, box_bm(0.40, 0.4, 0.04, M(2.42, 0, 0.585), MI['Grille']))
    for s in (1, -1):
        # side intakes in the bumper corners
        merge_bm(bm, box_bm(0.26, 0.5, 0.11, M(2.36, s * 0.66, 0.37, rz=s * 22), MI['Grille']))
        # headlight sockets: long, slanted, sweeping back along the fender
        merge_bm(bm, box_bm(*HL_SIZE, HL(s), MI['Reflector']))
        # side window openings (the dark walls behind the glass read as the cabin)
        merge_bm(bm, window_cutter_bm(WIN_FRONT, s))
        merge_bm(bm, window_cutter_bm(WIN_REAR, s))
        # fender vents behind the front wheels
        for k in range(2):
            merge_bm(bm, box_bm(0.2, 0.025, 0.17, M(0.88 - k * 0.06, s * 1.02, 0.62, rx=25), MI['Trim']))
        # tail-light pods
        for w in (0.46, 0.72):
            merge_bm(bm, cyl_bm(0.088, 0.3, 32, M(-2.47, s * w, 0.80, rx=90), MI['TailLens']))
    # rear diffuser
    merge_bm(bm, box_bm(1.36, 0.6, 0.15, M(-2.55, 0, 0.27), MI['Carbon']))
    # panel shut lines (6 mm grooves): doors, hood, trunk -> read as real body panels once AO is baked
    SEAM, sm = 0.006, MI['WellLiner']
    for s in (1, -1):
        for L in (0.72, -0.24):                                                       # door front / rear edges
            merge_bm(bm, box_bm(0.22, SEAM, 0.60, M(L, s * 1.0, 0.64), sm))
        merge_bm(bm, box_bm(0.22, 0.96, SEAM, M(0.24, s * 1.0, 0.36), sm))            # door bottom edge
        merge_bm(bm, box_bm(SEAM, 1.2, 0.30, M(1.62, s * 0.80, 0.86), sm))            # hood edges along the fenders
        merge_bm(bm, box_bm(SEAM, 0.55, 0.20, M(-2.02, s * 0.72, 0.95), sm))          # trunk lid sides
    merge_bm(bm, box_bm(1.6, SEAM, 0.25, M(1.02, 0, 0.93), sm))                        # hood rear edge (cowl)
    merge_bm(bm, box_bm(0.9, SEAM, 0.20, M(2.24, 0, 0.72), sm))                        # hood front edge
    merge_bm(bm, box_bm(1.44, SEAM, 0.20, M(-1.745, 0, 0.98), sm))                     # trunk front edge
    merge_bm(bm, box_bm(1.44, 0.20, SEAM, M(-2.33, 0, 0.865), sm))                     # trunk rear edge
    ob = new_obj('Cutters', bm, list(MI.keys()))
    return ob


def boolean(ob, cutter):
    mod = ob.modifiers.new('bool', 'BOOLEAN')
    mod.operation = 'DIFFERENCE'
    mod.solver = 'EXACT'
    mod.object = cutter
    mod.material_mode = 'TRANSFER'
    mod.use_self = True
    apply_modifiers(ob)


# ------------------------------------------------------------------ details joined into the body
def build_details():
    bm = bmesh.new()
    for s in (1, -1):
        hl = HL(s)
        # sunk chrome reflector bowl, LED brow and twin projectors inside the socket (seen through the lens)
        merge_bm(bm, box_bm(0.33, 0.41, 0.02, hl @ Matrix.Translation((0, 0, -0.04)), MI['Reflector']))
        merge_bm(bm, box_bm(0.29, 0.014, 0.018, hl @ Matrix.Translation((0, -0.17, -0.005)), MI['HeadLED']))
        for off in (-0.085, 0.035):
            merge_bm(bm, cyl_bm(0.034, 0.035, 20, hl @ Matrix.Translation((s * off, -0.13, -0.02)) @ Matrix.Rotation(math.radians(90), 4, 'X'), MI['HeadLED']))
            merge_bm(bm, cyl_bm(0.046, 0.03, 20, hl @ Matrix.Translation((s * off, -0.115, -0.02)) @ Matrix.Rotation(math.radians(90), 4, 'X'), MI['Chrome']))
        # side intake fins / grille bars
        merge_bm(bm, box_bm(0.24, 0.02, 0.014, M(2.30, s * 0.66, 0.37, rz=s * 22), MI['Chrome']))
        merge_bm(bm, box_bm(0.12, 0.05, 0.025, M(0.74, s * 0.89, 0.975), MI['Trim']))   # mirror stalk
        # door handle
        merge_bm(bm, box_bm(0.012, 0.16, 0.025, M(-0.04, s * 0.952, 0.92), MI['Chrome']))
        for w in (0.46, 0.72):
            ring = bmesh.new()
            bmesh.ops.create_circle(ring, cap_ends=False, segments=32, radius=0.074)
            geom = ring.edges[:]
            ext = bmesh.ops.extrude_edge_only(ring, edges=geom)
            vs = [v for v in ext['geom'] if isinstance(v, bmesh.types.BMVert)]
            bmesh.ops.scale(ring, vec=(0.72, 0.72, 1), verts=vs, space=Matrix())
            for f in ring.faces:
                f.material_index = MI['TailLED']
            transform_bm(ring, M(-2.335, s * w, 0.80, rx=90))
            merge_bm(bm, ring)
            merge_bm(bm, cyl_bm(0.054, 0.012, 24, M(-2.315, s * w, 0.80, rx=90), MI['TailLens']))
            merge_bm(bm, cyl_bm(0.016, 0.012, 12, M(-2.33, s * w, 0.80, rx=90), MI['TailLED']))
        # quad exhaust (titanium tips poking just out of the diffuser)
        for w in (0.47, 0.63):
            merge_bm(bm, cyl_bm(0.052, 0.16, 24, M(-2.30, s * w, 0.29, rx=90), MI['Exhaust']))
            merge_bm(bm, cyl_bm(0.043, 0.17, 24, M(-2.30, s * w, 0.29, rx=90), MI['WellLiner']))
    # diffuser fins (inside the recess)
    for w in (-0.30, -0.10, 0.10, 0.30):
        merge_bm(bm, box_bm(0.012, 0.12, 0.08, M(-2.19, w, 0.27), MI['Carbon']))
    merge_bm(bm, side_glass_bm())
    # underbody: flat armoured floor, front skid plate and reinforcement rails (visible over jumps)
    merge_bm(bm, box_bm(1.5, 3.9, 0.02, M(0, 0, 0.215), MI['Underbody']))
    merge_bm(bm, box_bm(1.3, 0.55, 0.014, M(1.95, 0, 0.235, rx=8), MI['Underbody']))
    for w in (-0.62, -0.32, 0.32, 0.62):
        merge_bm(bm, box_bm(0.05, 3.4, 0.035, M(0, w, 0.2), MI['Underbody']))
    for L in (-1.0, 0.0, 1.0):
        merge_bm(bm, box_bm(1.3, 0.05, 0.03, M(L, 0, 0.198), MI['Underbody']))
    # grille mesh bars
    for k in range(6):
        z = 0.33 + k * 0.035
        merge_bm(bm, box_bm(0.62 + k * 0.03, 0.012, 0.008, M(2.16, 0, z, rx=0), MI['Chrome'] if k in (0, 5) else MI['Grille']))
    # roof shark fin antenna
    fin = bmesh.new()
    vs = [fin.verts.new(V(L, 0, H)) for (L, H) in ((-0.7, 1.345), (-0.92, 1.33), (-0.88, 1.395))]
    fin.faces.new(vs)
    ex = bmesh.ops.extrude_face_region(fin, geom=fin.faces[:])
    bmesh.ops.translate(fin, vec=(0.035, 0, 0), verts=[v for v in ex['geom'] if isinstance(v, bmesh.types.BMVert)])
    bmesh.ops.translate(fin, vec=(-0.0175, 0, 0), verts=fin.verts)
    for f in fin.faces:
        f.material_index = MI['Paint']
    merge_bm(bm, fin)
    return new_obj('Details', bm, list(MI.keys()))


def build_mirrors():
    bm = bmesh.new()
    for s in (1, -1):
        merge_bm(bm, box_bm(0.21, 0.13, 0.115, M(0.70, s * 1.02, 1.0, rz=s * -8), 0))
    ob = new_obj('Mirrors', bm, ['Paint'])
    sub = ob.modifiers.new('sub', 'SUBSURF')
    sub.levels = 2
    apply_modifiers(ob)
    return ob


def build_lenses(body):
    """headlight lenses = socket volume intersected with the uncut body -> perfectly flush with the paint"""
    src = body.copy()
    src.data = body.data.copy()
    scene.collection.objects.link(src)
    lenses = []
    for s in (1, -1):
        sx, sy, sz = HL_SIZE
        lens = new_obj('Lens', box_bm(sx - 0.006, sy - 0.006, sz - 0.006, HL(s), 0), ['HeadLens'])
        mod = lens.modifiers.new('i', 'BOOLEAN')
        mod.operation = 'INTERSECT'
        mod.solver = 'EXACT'
        mod.object = src
        apply_modifiers(lens)
        while len(lens.data.materials) > 1:
            lens.data.materials.pop()
        for p in lens.data.polygons:
            p.material_index = 0
        lenses.append(lens)
    bpy.data.objects.remove(src)
    return lenses


# ------------------------------------------------------------------ variants
def airfoil_bm(chord, thick, span, mtx, mat_index):
    pts = []
    for i in range(13):
        x = i / 12
        yt = 5 * thick * (0.2969 * math.sqrt(x) - 0.126 * x - 0.3516 * x ** 2 + 0.2843 * x ** 3 - 0.1036 * x ** 4)
        pts.append((x, yt))
    up = [(x * chord, y * chord) for x, y in pts]
    lo = [(x * chord, -y * chord * 0.4) for x, y in reversed(pts[1:-1])]
    prof = up + lo
    bm = bmesh.new()
    a = [bm.verts.new((-span / 2, (L - chord / 2), H)) for (L, H) in prof]   # leading edge faces forward (+L = -y)
    b = [bm.verts.new((span / 2, (L - chord / 2), H)) for (L, H) in prof]
    n = len(prof)
    for k in range(n):
        bm.faces.new((a[k], a[(k + 1) % n], b[(k + 1) % n], b[k]))
    bm.faces.new(a)
    bm.faces.new(list(reversed(b)))
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    transform_bm(bm, mtx)
    for f in bm.faces:
        f.material_index = mat_index
    return bm


def build_variants():
    objs = []
    # stock trunk wing: two pedestals + slim body-colour blade
    bm = airfoil_bm(0.26, 0.10, 1.48, M(-2.06, 0, 1.06, rx=-4), 0)
    for s in (1, -1):
        merge_bm(bm, box_bm(0.035, 0.12, 0.10, M(-2.04, s * 0.48, 1.0, rx=-10), 1))
    ob = new_obj('Wing_Std', bm, ['Paint', 'Trim'])
    objs.append(ob)
    # time-attack GT wing with swan-neck mounts & end plates
    bm = airfoil_bm(0.42, 0.12, 1.95, M(-2.1, 0, 1.32, rx=-8), 0)
    for s in (1, -1):
        merge_bm(bm, box_bm(0.03, 0.10, 0.34, M(-2.0, s * 0.45, 1.13, rx=-18), 0))
        merge_bm(bm, box_bm(0.012, 0.52, 0.24, M(-2.1, s * 0.98, 1.3), 0))
    objs.append(new_obj('Wing_GT', bm, ['Carbon']))
    bm = box_bm(1.92, 0.3, 0.022, M(2.30, 0, 0.235), 0)
    for s in (1, -1):
        merge_bm(bm, box_bm(0.16, 0.20, 0.014, M(2.18, s * 0.86, 0.47, ry=s * 18), 0))
    objs.append(new_obj('Aero_GT', bm, ['Carbon']))
    # heavy: bull bar + roof light bar
    bm = bmesh.new()
    for s in (1, -1):
        merge_bm(bm, cyl_bm(0.045, 0.62, 12, M(2.52, s * 0.5, 0.58), 0))
    for z in (0.86, 0.40):
        merge_bm(bm, cyl_bm(0.045, 1.22, 12, M(2.52, 0, z, ry=90), 0))
    merge_bm(bm, box_bm(1.9, 0.32, 0.14, M(2.33, 0, 0.30), 1))
    objs.append(new_obj('BullBar', bm, ['Chrome', 'Trim']))
    bm = box_bm(1.0, 0.12, 0.08, M(0.15, 0, 1.385), 0)
    for w in (-0.36, -0.12, 0.12, 0.36):
        merge_bm(bm, box_bm(0.17, 0.02, 0.06, M(0.215, w, 1.385), 1))
    objs.append(new_obj('LightBar', bm, ['Trim', 'Amber']))
    return objs


# ------------------------------------------------------------------ wheel (axis = blender X)
R = 0.36


def lathe(profile, segs, mat_index, rfun=None):
    """profile: list of (x_along_axis, radius) -> surface of revolution around X (rfun can carve a pattern)"""
    bm = bmesh.new()
    rings = []
    for i in range(segs):
        a = 2 * math.pi * i / segs
        rings.append([bm.verts.new((x, (rfun(i, x, r) if rfun else r) * math.cos(a), (rfun(i, x, r) if rfun else r) * math.sin(a))) for (x, r) in profile])
    n = len(profile)
    for i in range(segs):
        A, B = rings[i], rings[(i + 1) % segs]
        for k in range(n - 1):
            bm.faces.new((A[k], A[k + 1], B[k + 1], B[k]))
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    for f in bm.faces:
        f.material_index = mat_index
    return bm


def build_wheel():
    tire = [(-0.135, 0.258), (-0.148, 0.29), (-0.152, 0.325), (-0.145, 0.348), (-0.125, R), (-0.075, R), (-0.068, R - 0.008), (-0.058, R - 0.008),
            (-0.05, R), (-0.008, R), (-0.002, R - 0.008), (0.008, R - 0.008), (0.014, R), (0.05, R), (0.058, R - 0.008), (0.068, R - 0.008), (0.075, R),
            (0.125, R), (0.145, 0.348), (0.152, 0.325), (0.148, 0.29), (0.135, 0.258)]
    def tread(i, x, r):   # directional tread: angled shoulder blocks + staggered sipes between the circumferential grooves
        if r < R - 1e-4:
            return r
        ax = abs(x)
        if ax > 0.078 and (i + int(ax * 70)) % 5 == 0:
            return r - 0.009
        if 0.016 < ax < 0.05 and (i + int(ax * 90)) % 7 == 0:
            return r - 0.006
        return r
    # extra samples across the tread face so the pattern has resolution (tread section is x-sorted: indices 4..17)
    face = sorted(tire[4:18] + [(-0.11, R), (-0.095, R), (-0.03, R), (0.03, R), (0.095, R), (0.11, R)], key=lambda p: p[0])
    bm = lathe(tire[:4] + face + tire[18:], 96, 0, tread)
    rim = [(-0.13, 0.252), (-0.12, 0.236), (0.10, 0.236), (0.14, 0.246), (0.152, 0.258), (0.16, 0.25), (0.145, 0.232), (0.11, 0.222)]
    merge_bm(bm, lathe(rim, 48, 1))
    # 6 twin spokes, slightly concave towards the hub
    for k in range(6):
        for d in (-6.5, 6.5):
            a = math.radians(k * 60 + d)
            sp = box_bm(0.035, 0.026, 0.19, Matrix.Rotation(a, 4, 'X') @ Matrix.Translation((0.10, 0, 0.14)) @ Matrix.Rotation(math.radians(-14), 4, 'Y'), 1)
            merge_bm(bm, sp)
    merge_bm(bm, cyl_bm(0.075, 0.07, 32, Matrix.Translation((0.085, 0, 0)) @ Matrix.Rotation(math.radians(90), 4, 'Y'), 1))
    for k in range(5):
        a = 2 * math.pi * k / 5
        merge_bm(bm, cyl_bm(0.011, 0.03, 8, Matrix.Translation((0.125, 0.045 * math.cos(a), 0.045 * math.sin(a))) @ Matrix.Rotation(math.radians(90), 4, 'Y'), 2))
    merge_bm(bm, cyl_bm(0.026, 0.02, 16, Matrix.Translation((0.13, 0, 0)) @ Matrix.Rotation(math.radians(90), 4, 'Y'), 2))
    # brake disc (with cross-drilled look via dark hat)
    merge_bm(bm, cyl_bm(0.19, 0.03, 48, Matrix.Translation((-0.02, 0, 0)) @ Matrix.Rotation(math.radians(90), 4, 'Y'), 3))
    merge_bm(bm, cyl_bm(0.09, 0.06, 24, Matrix.Translation((0.02, 0, 0)) @ Matrix.Rotation(math.radians(90), 4, 'Y'), 1))
    ob = new_obj('Wheel', bm, ['Tire', 'Rim', 'Chrome', 'Brake'])
    smooth_all(ob, 40)
    cal = box_bm(0.07, 0.11, 0.2, Matrix.Rotation(math.radians(-35), 4, 'X') @ Matrix.Translation((0.0, 0, 0.165)), 0)
    co = new_obj('Caliper', cal, ['Caliper'])
    sub = co.modifiers.new('sub', 'SUBSURF')
    sub.levels = 1
    apply_modifiers(co)
    smooth_all(co)
    return ob, co


# ------------------------------------------------------------------ AO bake into vertex colours
def bake_ao(body):
    scene.render.engine = 'CYCLES'
    scene.cycles.samples = 48
    scene.cycles.device = 'CPU'
    world = bpy.data.worlds.new('W')
    scene.world = world
    world.light_settings.distance = 0.55
    # ground so the lower body gets contact occlusion
    gbm = bmesh.new()
    bmesh.ops.create_grid(gbm, x_segments=1, y_segments=1, size=6)
    ground = new_obj('Ground', gbm, ['Trim'])
    me = body.data
    attr = me.color_attributes.new('AO', 'BYTE_COLOR', 'CORNER')
    me.color_attributes.active_color = attr
    for o in scene.objects:
        o.select_set(False)
    body.select_set(True)
    bpy.context.view_layer.objects.active = body
    bpy.ops.object.bake(type='AO', target='VERTEX_COLORS')
    # lift & soften so it reads as ambient occlusion, not dirt
    for d in attr.data:
        c = d.color
        a = 0.22 + 0.78 * (c[0] ** 0.8)
        d.color = (a, a, a, 1.0)
    bpy.data.objects.remove(ground)


# ------------------------------------------------------------------ build
body = build_body()
assign_regions(body)
lenses = build_lenses(body)
cut = build_cutters()
boolean(body, cut)
bpy.data.objects.remove(cut)
det = build_details()
mirrors = build_mirrors()
# join details into the body (materials are matched by datablock)
for o in scene.objects:
    o.select_set(False)
for o in [det, mirrors] + lenses:
    o.select_set(True)
body.select_set(True)
bpy.context.view_layer.objects.active = body
bpy.ops.object.join()
smooth_all(body, 38)
variants = build_variants()
for v in variants:
    smooth_all(v, 35)
wheel, caliper = build_wheel()
# wheels sit in the arches for the preview / AO, then go back to the origin for export
wheel_copies = []
for (L, s) in ((1.39, 1), (1.39, -1), (-1.39, 1), (-1.39, -1)):
    w = wheel.copy()
    w.data = wheel.data
    scene.collection.objects.link(w)
    w.location = V(L, s * 0.83, R)
    if s < 0:
        w.rotation_euler = (0, 0, math.pi)
    wheel_copies.append(w)
wheel.hide_render = True
caliper.hide_render = True
for v in variants:
    v.hide_render = True
bake_ao(body)
print('BODY TRIS', sum(len(p.vertices) - 2 for p in body.data.polygons))


def render_previews():
    for v in variants:
        v.hide_render = v.name not in ('Wing_Std',)
    scene.render.engine = 'BLENDER_WORKBENCH'
    sh = scene.display.shading
    sh.light = 'STUDIO'
    sh.color_type = 'MATERIAL'
    sh.show_cavity = True
    sh.show_shadows = True
    scene.render.resolution_x, scene.render.resolution_y = 1100, 620
    cam_data = bpy.data.cameras.new('cam')
    cam_data.lens = 50
    cam = bpy.data.objects.new('cam', cam_data)
    scene.collection.objects.link(cam)
    scene.camera = cam
    target = V(0, 0, 0.6)
    for name, (L, W, H) in {'front34': (5.2, 4.2, 1.9), 'side': (0.0, 7.6, 1.2), 'rear34': (-5.0, -4.0, 1.9), 'front': (7.5, 0.0, 1.0), 'rear': (-7.5, 0.0, 1.2), 'top': (0.01, 0.0, 9.0)}.items():
        cam.location = V(L, W, H)
        d = target - cam.location
        cam.rotation_euler = d.to_track_quat('-Z', 'Y').to_euler()
        scene.render.filepath = os.path.join(PREVIEW_DIR, f'car_{name}.png')
        bpy.ops.render.render(write_still=True)
    bpy.data.objects.remove(cam)


if PREVIEW_DIR:
    os.makedirs(PREVIEW_DIR, exist_ok=True)
    render_previews()

# ------------------------------------------------------------------ LODs for distant cars (AO colours survive the collapse)
def make_lod(src, name, ratio):
    ob = src.copy()
    ob.data = src.data.copy()
    ob.name = name
    scene.collection.objects.link(ob)
    dec = ob.modifiers.new('dec', 'DECIMATE')
    dec.decimate_type = 'COLLAPSE'
    dec.ratio = ratio
    apply_modifiers(ob)
    smooth_all(ob, 40)
    return ob


for w in wheel_copies:
    bpy.data.objects.remove(w)
body_lod = make_lod(body, 'Body_LOD', 0.2)
wheel_lod = make_lod(wheel, 'Wheel_LOD', 0.18)
print('LOD TRIS', sum(len(p.vertices) - 2 for p in body_lod.data.polygons), sum(len(p.vertices) - 2 for p in wheel_lod.data.polygons))

# ------------------------------------------------------------------ export
wheel.hide_render = False
for o in scene.objects:
    o.select_set(o.type == 'MESH')
os.makedirs(OUT_DIR, exist_ok=True)
out = os.path.join(OUT_DIR, 'car.glb')
bpy.ops.export_scene.gltf(
    filepath=out, export_format='GLB', use_selection=True, export_apply=True, export_yup=True,
    export_normals=True, export_texcoords=False, export_tangents=False, export_materials='EXPORT',
    export_vertex_color='NAME', export_vertex_color_name='AO', export_all_vertex_colors=False,
)
print('EXPORTED', out, os.path.getsize(out))
