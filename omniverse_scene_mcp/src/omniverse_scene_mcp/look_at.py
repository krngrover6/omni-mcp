"""Look-at math: rotations that point a prim's local -Z axis from an eye position at a target.

USD cameras and the UsdLux rect, disk and distant lights look (or emit) along their local -Z axis, with local +Y
as "up". This module is pure standard library so it runs on any Python, inside or outside Kit.

Conventions match USD's Gf types: matrices are row-vector, so a direction ``v`` (a row) maps to ``v * M`` and
the rows of a rotation matrix are the images of the local X, Y and Z axes. Euler angles are in degrees and
follow the xformOp:rotateABC ops: ``rotateXYZ`` turns about X first, then Y, then Z (M = Rx * Ry * Rz).
Quaternions are (w, x, y, z) with w >= 0.
"""

from __future__ import annotations

import math
from typing import Any, Sequence

ROTATION_ORDERS = ("XYZ", "XZY", "YXZ", "YZX", "ZXY", "ZYX")
UP_VECTORS = {"Y": (0.0, 1.0, 0.0), "Z": (0.0, 0.0, 1.0)}
# Direction a level view faces on each stage: a Y-up camera looks down -Z, a Z-up camera rotated 90 about X
# looks down +Y. Used as the fallback up vector when looking straight up or down.
HORIZON_FORWARD = {"Y": (0.0, 0.0, -1.0), "Z": (0.0, 1.0, 0.0)}
COINCIDENT_TOLERANCE = 1e-9
PARALLEL_TOLERANCE = 1e-6
GIMBAL_TOLERANCE = 1e-6

Vec3 = tuple[float, float, float]
Matrix3 = tuple[Vec3, Vec3, Vec3]


def normalize_up_axis(up_axis: Any) -> str:
    """Return 'Y' or 'Z' for a stage up axis token, raising ValueError for anything else."""
    text = str(up_axis).strip().upper() if up_axis is not None else ""
    if text not in UP_VECTORS:
        raise ValueError(f"up_axis must be 'Y' or 'Z', got {up_axis!r}")
    return text


def look_at_rotation(
    eye: Sequence[float],
    target: Sequence[float],
    up_axis: str | Sequence[float] = "Z",
    *,
    horizon_forward: Sequence[float] | None = None,
) -> dict[str, Any]:
    """Rotation that turns local -Z from eye toward target, keeping local +Y as close to the up axis as possible.

    up_axis is 'Y', 'Z' or an up vector. When the view is (nearly) parallel to up, the up vector falls back to
    horizon_forward when looking down and to its negation when looking up, so the camera keeps a stable roll.
    eye == target is reported as degenerate with the identity rotation.
    Returns JSON-safe data: rotate_xyz (degrees), quaternion (w, x, y, z), matrix (rows), forward, up, right,
    distance, degenerate, fallback_up, check (local -Z transformed by the matrix) and warnings.
    """
    eye_v = _vec3(eye, "eye")
    target_v = _vec3(target, "target")
    if isinstance(up_axis, str):
        axis_name = normalize_up_axis(up_axis)
        up = UP_VECTORS[axis_name]
        default_horizon = HORIZON_FORWARD[axis_name]
    else:
        axis_name = "custom"
        up = _unit(_vec3(up_axis, "up_axis"), "up_axis")
        default_horizon = _perpendicular_axis(up)
    horizon = _unit(_vec3(horizon_forward, "horizon_forward"), "horizon_forward") if horizon_forward is not None else default_horizon

    warnings: list[str] = []
    offset = _sub(target_v, eye_v)
    distance = _length(offset)
    scale = max(1.0, _length(eye_v), _length(target_v))
    if distance <= COINCIDENT_TOLERANCE * scale:
        warnings.append("eye and target are the same point, so there is no direction to look along; returned the identity rotation.")
        rows: Matrix3 = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
        return _result(rows, axis_name, distance, True, None, warnings)

    forward = _scale(offset, 1.0 / distance)
    rows, fallback = frame_from_forward(forward, up, horizon)
    if fallback is not None:
        warnings.append(
            "The target is straight above or below the eye, so the up axis is parallel to the view; "
            f"used {_clean_vec(fallback)} as the up vector to keep a stable roll."
        )
    return _result(rows, axis_name, distance, False, fallback, warnings)


def frame_from_forward(
    forward: Sequence[float], up: Sequence[float], horizon_forward: Sequence[float]
) -> tuple[Matrix3, Vec3 | None]:
    """Orthonormal rows (right, true up, -forward) for a unit forward vector; also returns the fallback up used."""
    f = _unit(_vec3(forward, "forward"), "forward")
    up_v = _unit(_vec3(up, "up"), "up")
    fallback: Vec3 | None = None
    right = _cross(f, up_v)
    if _length(right) < PARALLEL_TOLERANCE:
        horizon = _unit(_vec3(horizon_forward, "horizon_forward"), "horizon_forward")
        fallback = horizon if _dot(f, up_v) < 0.0 else _scale(horizon, -1.0)
        right = _cross(f, fallback)
        if _length(right) < PARALLEL_TOLERANCE:  # horizon_forward was itself parallel to the view
            fallback = _perpendicular_axis(f)
            right = _cross(f, fallback)
    right = _unit(right, "right")
    true_up = _cross(right, f)
    return (right, true_up, _scale(f, -1.0)), fallback


def euler_to_matrix(angles: Sequence[float], order: str = "XYZ") -> Matrix3:
    """Row-vector rotation matrix of an xformOp:rotate<order> op.

    As in USD, angles are always (about X, about Y, about Z) whatever the order; the order only says which
    axis turns first (rotateZYX turns about Z, then Y, then X).
    """
    order = _order(order)
    values = _vec3(angles, "angles")
    matrix: Matrix3 = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    for axis_name in order:
        axis = "XYZ".index(axis_name)
        matrix = _matmul(matrix, _axis_matrix(axis, values[axis]))
    return matrix


def matrix_to_euler(rows: Sequence[Sequence[float]], order: str = "XYZ") -> Vec3:
    """Angles (about X, Y, Z in degrees) for xformOp:rotate<order> that reproduce a rotation matrix.

    Gimbal-safe: at +-90 degrees on the middle axis the last axis gets 0 and the first takes the whole turn.
    """
    order = _order(order)
    m = _matrix(rows)
    i, j, k = ("XYZ".index(letter) for letter in order)
    sign = 1.0 if (i, j, k) in ((0, 1, 2), (1, 2, 0), (2, 0, 1)) else -1.0

    def col(r: int, c: int) -> float:  # column-vector matrix element
        return m[c][r]

    sin_b = max(-1.0, min(1.0, -sign * col(k, i)))
    b = math.asin(sin_b)
    if math.cos(b) > GIMBAL_TOLERANCE:
        a = math.atan2(sign * col(k, j), col(k, k))
        c = math.atan2(sign * col(j, i), col(i, i))
    else:  # gimbal lock: the first and last axes line up, so put the whole turn in the first angle
        a = math.atan2(-sign * col(j, k), col(j, j))
        c = 0.0
    angles = [0.0, 0.0, 0.0]
    angles[i], angles[j], angles[k] = math.degrees(a) + 0.0, math.degrees(b) + 0.0, math.degrees(c) + 0.0
    return (angles[0], angles[1], angles[2])


def matrix_to_quaternion(rows: Sequence[Sequence[float]]) -> tuple[float, float, float, float]:
    """Unit quaternion (w, x, y, z), w >= 0, for a row-vector rotation matrix."""
    m = _matrix(rows)

    def col(r: int, c: int) -> float:
        return m[c][r]

    trace = col(0, 0) + col(1, 1) + col(2, 2)
    if trace > 0.0:
        s = math.sqrt(trace + 1.0) * 2.0
        w, x, y, z = 0.25 * s, (col(2, 1) - col(1, 2)) / s, (col(0, 2) - col(2, 0)) / s, (col(1, 0) - col(0, 1)) / s
    elif col(0, 0) > col(1, 1) and col(0, 0) > col(2, 2):
        s = math.sqrt(1.0 + col(0, 0) - col(1, 1) - col(2, 2)) * 2.0
        w, x, y, z = (col(2, 1) - col(1, 2)) / s, 0.25 * s, (col(0, 1) + col(1, 0)) / s, (col(0, 2) + col(2, 0)) / s
    elif col(1, 1) > col(2, 2):
        s = math.sqrt(1.0 + col(1, 1) - col(0, 0) - col(2, 2)) * 2.0
        w, x, y, z = (col(0, 2) - col(2, 0)) / s, (col(0, 1) + col(1, 0)) / s, 0.25 * s, (col(1, 2) + col(2, 1)) / s
    else:
        s = math.sqrt(1.0 + col(2, 2) - col(0, 0) - col(1, 1)) * 2.0
        w, x, y, z = (col(1, 0) - col(0, 1)) / s, (col(0, 2) + col(2, 0)) / s, (col(1, 2) + col(2, 1)) / s, 0.25 * s
    norm = math.sqrt(w * w + x * x + y * y + z * z) or 1.0
    if w < 0.0:
        norm = -norm
    return (w / norm, x / norm, y / norm, z / norm)


def quaternion_to_matrix(quat: Sequence[float]) -> Matrix3:
    """Row-vector rotation matrix of a quaternion (w, x, y, z); the quaternion is normalized first."""
    if len(quat) != 4:
        raise ValueError("quaternion must have 4 components (w, x, y, z)")
    w, x, y, z = (float(value) for value in quat)
    norm = math.sqrt(w * w + x * x + y * y + z * z)
    if not math.isfinite(norm) or norm < COINCIDENT_TOLERANCE:
        raise ValueError("quaternion must be finite and non-zero")
    w, x, y, z = w / norm, x / norm, y / norm, z / norm
    column = (
        (1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)),
        (2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)),
        (2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)),
    )
    return _transpose(column)


def transform_direction(rows: Sequence[Sequence[float]], direction: Sequence[float]) -> Vec3:
    """Row-vector product ``direction * M`` (what Gf.Matrix4d.TransformDir does for the 3x3 part)."""
    m = _matrix(rows)
    v = _vec3(direction, "direction")
    return tuple(v[0] * m[0][c] + v[1] * m[1][c] + v[2] * m[2][c] for c in range(3))  # type: ignore[return-value]


def forward_of(rows: Sequence[Sequence[float]]) -> Vec3:
    """Where the local -Z axis points after the rotation."""
    return transform_direction(rows, (0.0, 0.0, -1.0))


def angle_between(first: Sequence[float], second: Sequence[float]) -> float:
    """Angle in degrees between two non-zero vectors."""
    a = _unit(_vec3(first, "first"), "first")
    b = _unit(_vec3(second, "second"), "second")
    # atan2 of |cross| and dot stays accurate for tiny angles, unlike acos.
    return math.degrees(math.atan2(_length(_cross(a, b)), _dot(a, b)))


def _result(
    rows: Matrix3, axis_name: str, distance: float, degenerate: bool, fallback: Vec3 | None, warnings: list[str]
) -> dict[str, Any]:
    check = forward_of(rows)
    return {
        "rotate_xyz": _clean_vec(matrix_to_euler(rows, "XYZ")),
        "quaternion": _clean_vec(matrix_to_quaternion(rows)),
        "matrix": [_clean_vec(row) for row in rows],
        "forward": _clean_vec(check),
        "up": _clean_vec(rows[1]),
        "right": _clean_vec(rows[0]),
        "up_axis": axis_name,
        "distance": _clean(distance),
        "degenerate": degenerate,
        "fallback_up": _clean_vec(fallback) if fallback is not None else None,
        "check": {"local_minus_z_becomes": _clean_vec(check)},
        "warnings": warnings,
    }


def _axis_matrix(axis: int, degrees: float) -> Matrix3:
    """Row-vector rotation about one principal axis (counter-clockwise looking down the axis)."""
    radians = math.radians(degrees)
    c, s = math.cos(radians), math.sin(radians)
    if axis == 0:
        return ((1.0, 0.0, 0.0), (0.0, c, s), (0.0, -s, c))
    if axis == 1:
        return ((c, 0.0, -s), (0.0, 1.0, 0.0), (s, 0.0, c))
    return ((c, s, 0.0), (-s, c, 0.0), (0.0, 0.0, 1.0))


def _perpendicular_axis(vector: Vec3) -> Vec3:
    """A unit vector perpendicular to vector, built from the principal axis it is least aligned with."""
    v = _unit(vector, "vector")
    axis_index = min(range(3), key=lambda index: abs(v[index]))
    axis = tuple(1.0 if index == axis_index else 0.0 for index in range(3))
    projected = _sub(axis, _scale(v, _dot(axis, v)))  # type: ignore[arg-type]
    return _unit(projected, "perpendicular")


def _order(order: str) -> str:
    text = str(order).strip().upper()
    if text not in ROTATION_ORDERS:
        raise ValueError(f"rotation order must be one of {ROTATION_ORDERS}, got {order!r}")
    return text


def _matrix(rows: Sequence[Sequence[float]]) -> Matrix3:
    if len(rows) != 3:
        raise ValueError("matrix must have 3 rows")
    return tuple(_vec3(row, "matrix row") for row in rows)  # type: ignore[return-value]


def _vec3(value: Any, name: str) -> Vec3:
    try:
        items = [value[index] for index in range(len(value))]
    except TypeError:
        raise ValueError(f"{name} must be a sequence of 3 numbers") from None
    if len(items) != 3 or any(isinstance(item, bool) for item in items):
        raise ValueError(f"{name} must be a sequence of 3 numbers")
    try:
        numbers = tuple(float(item) for item in items)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a sequence of 3 numbers") from None
    if not all(math.isfinite(number) for number in numbers):
        raise ValueError(f"{name} must contain finite numbers")
    return numbers  # type: ignore[return-value]


def _unit(vector: Vec3, name: str) -> Vec3:
    length = _length(vector)
    if length < COINCIDENT_TOLERANCE:
        raise ValueError(f"{name} must not be a zero vector")
    return _scale(vector, 1.0 / length)


def _matmul(first: Matrix3, second: Matrix3) -> Matrix3:
    return tuple(
        tuple(sum(first[r][k] * second[k][c] for k in range(3)) for c in range(3)) for r in range(3)
    )  # type: ignore[return-value]


def _transpose(matrix: Sequence[Sequence[float]]) -> Matrix3:
    return tuple(tuple(float(matrix[r][c]) for r in range(3)) for c in range(3))  # type: ignore[return-value]


def _dot(a: Vec3, b: Vec3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a: Vec3, b: Vec3) -> Vec3:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _sub(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _scale(a: Vec3, factor: float) -> Vec3:
    return (a[0] * factor, a[1] * factor, a[2] * factor)


def _length(a: Vec3) -> float:
    return math.sqrt(_dot(a, a))


def _clean(value: float) -> float:
    rounded = round(float(value), 9)
    return 0.0 if rounded == 0.0 else rounded


def _clean_vec(values: Sequence[float]) -> list[float]:
    return [_clean(value) for value in values]
