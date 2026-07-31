// Geometry checks. Run directly (`node tests/test_geometry.js`) or via pytest,
// which shells out to node and skips if node isn't installed.

const assert = require("assert");
const G = require("../visual/static/geometry.js");

const cell = (x, y, is_enemy = false, name = "") => ({ x, y, is_enemy, name });

// --- movement: Chebyshev, diagonals are free ---
assert.strictEqual(G.feetBetween(cell(0, 0), cell(0, 1)), 5, "one step");
assert.strictEqual(G.feetBetween(cell(0, 0), cell(1, 1)), 5, "a diagonal costs one step");
assert.strictEqual(G.feetBetween(cell(2, 2), cell(8, 4)), 30, "six across, two down");
assert.strictEqual(G.feetBetween(cell(5, 5), cell(5, 5)), 0, "same square");
assert.strictEqual(G.feetBetween(cell(0, 0), cell(0, 2), 10), 20, "scale is honoured");

// --- reach: a square, not a circle ---
const reach = G.reachBox(cell(6, 6), 30);
assert.strictEqual(reach.squares, 6);
assert.deepStrictEqual(
  [reach.minX, reach.maxX, reach.minY, reach.maxY], [0, 12, 0, 12]);
assert.strictEqual(G.reachBox(cell(0, 0), 25).squares, 5, "25 ft is five squares");
assert.strictEqual(G.reachBox(cell(0, 0), 27).squares, 5, "partial squares don't count");

// --- blast: Euclidean from centre to centre ---
const centre = cell(10, 10);
assert.ok(G.inBlast(cell(10, 10), centre, 20), "the centre square is in it");
assert.ok(G.inBlast(cell(14, 10), centre, 20), "20 ft out, exactly on the edge");
assert.ok(!G.inBlast(cell(15, 10), centre, 20), "25 ft out is clear");
// The corner of the bounding box is 28.3 ft away — outside a 20 ft sphere.
assert.ok(!G.inBlast(cell(14, 14), centre, 20), "a sphere is not a square");
assert.ok(G.inBlast(cell(12, 12), centre, 20), "14.1 ft diagonal is inside");

// --- the actual question: can the wizard fire without hitting the party? ---
const board = [
  cell(10, 10, true, "Cultist Leader"),
  cell(11, 10, true, "Sewer Cultist"),
  cell(12, 10, false, "Bram"),      // 10 ft from centre — caught
  cell(2, 2, false, "Oro"),         // far away — safe
];
const tight = G.blastReport(board, centre, 20);
assert.strictEqual(tight.enemies.length, 2);
assert.strictEqual(tight.allies.length, 1, "Bram is standing too close");
assert.deepStrictEqual(tight.allies.map((t) => t.name), ["Bram"]);

const small = G.blastReport(board, centre, 5);
assert.strictEqual(small.enemies.length, 2, "both cultists are within 5 ft");
assert.strictEqual(small.allies.length, 0, "a tighter blast spares Bram");

const empty = G.blastReport([], centre, 20);
assert.deepStrictEqual(empty.caught, []);

console.log("geometry: all assertions passed");
