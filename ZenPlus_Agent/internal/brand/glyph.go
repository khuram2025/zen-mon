package brand

import (
	"image"
	"image/color"
	"math"
)

// Glyph is the symbol inside the dashboard's status disc.
type Glyph int

const (
	GlyphCheck   Glyph = iota // healthy
	GlyphAlert                // needs attention
	GlyphCross                // failed
	GlyphPending              // waiting: connecting or pending approval
	GlyphStopped              // agent not running
)

var glyphColors = map[Glyph]color.RGBA{
	GlyphCheck:   {R: 22, G: 163, B: 74, A: 255},
	GlyphAlert:   statusColors[StatusWarning],
	GlyphCross:   statusColors[StatusError],
	GlyphPending: statusColors[StatusWarning],
	GlyphStopped: statusColors[StatusStopped],
}

// Strokes in a unit box; a zero-length stroke is a dot.
var glyphStrokes = map[Glyph][][4]float64{
	GlyphCheck:   {{0.29, 0.52, 0.44, 0.67}, {0.44, 0.67, 0.72, 0.37}},
	GlyphAlert:   {{0.5, 0.27, 0.5, 0.56}, {0.5, 0.72, 0.5, 0.72}},
	GlyphCross:   {{0.35, 0.35, 0.65, 0.65}, {0.65, 0.35, 0.35, 0.65}},
	GlyphPending: {{0.31, 0.5, 0.31, 0.5}, {0.5, 0.5, 0.5, 0.5}, {0.69, 0.5, 0.69, 0.5}},
	GlyphStopped: {{0.34, 0.5, 0.66, 0.5}},
}

// StatusGlyph returns a filled status disc with a white symbol, drawn with
// the same anti-aliasing as the tray badge so both read as one family.
func StatusGlyph(size int, g Glyph) image.Image {
	out := image.NewRGBA(image.Rect(0, 0, size, size))
	s := float64(size)
	drawDisc(out, s/2, s/2, s/2, glyphColors[g])
	width := s * 0.1
	if g == GlyphPending {
		width = s * 0.12
	}
	drawStrokes(out, glyphStrokes[g], s, width, color.RGBA{R: 255, G: 255, B: 255, A: 255})
	return out
}

// drawStrokes paints round-capped line segments given in unit coordinates.
func drawStrokes(img *image.RGBA, strokes [][4]float64, scale, width float64, c color.RGBA) {
	half := width / 2
	b := img.Bounds()
	for y := b.Min.Y; y < b.Max.Y; y++ {
		for x := b.Min.X; x < b.Max.X; x++ {
			covered := 0
			for sy := 0; sy < 4; sy++ {
				for sx := 0; sx < 4; sx++ {
					px := float64(x) + (float64(sx)+0.5)/4
					py := float64(y) + (float64(sy)+0.5)/4
					for _, st := range strokes {
						if segmentDistance(px, py, st[0]*scale, st[1]*scale, st[2]*scale, st[3]*scale) <= half {
							covered++
							break
						}
					}
				}
			}
			if covered == 0 {
				continue
			}
			alpha := float64(covered) / 16
			under := img.RGBAAt(x, y)
			img.SetRGBA(x, y, color.RGBA{
				R: blend(under.R, c.R, alpha),
				G: blend(under.G, c.G, alpha),
				B: blend(under.B, c.B, alpha),
				A: uint8(math.Min(255, float64(under.A)+(255-float64(under.A))*alpha)),
			})
		}
	}
}

func segmentDistance(px, py, ax, ay, bx, by float64) float64 {
	dx, dy := bx-ax, by-ay
	t := 0.0
	if l := dx*dx + dy*dy; l > 0 {
		t = math.Max(0, math.Min(1, ((px-ax)*dx+(py-ay)*dy)/l))
	}
	return math.Hypot(px-(ax+t*dx), py-(ay+t*dy))
}
