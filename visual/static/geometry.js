// Grid geometry for the tactical map. Pure functions, no DOM, no model.
//
// Two different distance rules on purpose, both straight out of 5e:
//
//   movement  — Chebyshev. A diagonal step costs the same as a straight one,
//               so "30 ft of movement" reaches a SQUARE of squares.
//   areas     — Euclidean between square centres. A 20-ft-radius sphere is a
//               CIRCLE, and catches whatever centre falls inside it.
//
// Drawing them differently is not an inconsistency; it's what the rules say.

const Geometry = {
  DEFAULT_FEET_PER_SQUARE: 5,

  // Distance a creature would have to move, in feet.
  feetBetween(a, b, feetPerSquare = Geometry.DEFAULT_FEET_PER_SQUARE) {
    return Math.max(Math.abs(a.x - b.x), Math.abs(a.y - b.y)) * feetPerSquare;
  },

  // Squares reachable with `speedFt` of movement — inclusive bounds.
  reachBox(origin, speedFt, feetPerSquare = Geometry.DEFAULT_FEET_PER_SQUARE) {
    const squares = Math.floor(speedFt / feetPerSquare);
    return {
      minX: origin.x - squares,
      maxX: origin.x + squares,
      minY: origin.y - squares,
      maxY: origin.y + squares,
      squares,
    };
  },

  // Is a square's centre inside a sphere centred on another square's centre?
  inBlast(cell, center, radiusFt, feetPerSquare = Geometry.DEFAULT_FEET_PER_SQUARE) {
    const dx = (cell.x - center.x) * feetPerSquare;
    const dy = (cell.y - center.y) * feetPerSquare;
    return Math.sqrt(dx * dx + dy * dy) <= radiusFt;
  },

  // Who gets hit, split so the caster can see the cost before committing.
  blastReport(tokens, center, radiusFt, feetPerSquare = Geometry.DEFAULT_FEET_PER_SQUARE) {
    const caught = tokens.filter((t) => Geometry.inBlast(t, center, radiusFt, feetPerSquare));
    return {
      caught,
      allies: caught.filter((t) => !t.is_enemy),
      enemies: caught.filter((t) => t.is_enemy),
    };
  },
};

// Browser gets the global; node's test runner gets the export.
if (typeof module !== "undefined" && module.exports) {
  module.exports = Geometry;
}
