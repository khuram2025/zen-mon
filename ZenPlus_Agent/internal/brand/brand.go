// Package brand provides the ZenPlus mark for window, header and tray icons,
// so every surface shows the same logo as the executable and installer icon.
package brand

import (
	"bytes"
	"embed"
	"fmt"
	"image"
	"image/color"
	"image/draw"
	"image/png"
	"math"
	"sort"
	"sync"
)

// The mark is pre-rendered from the 256 px frame of assets/zenplus-agent.ico
// at the sizes Windows asks for; Go has no high-quality image scaler.
//
//go:embed mark-*.png
var marks embed.FS

var markSizes = []int{16, 20, 24, 32, 48, 64, 96}

var cache sync.Map // size -> *image.RGBA

// Mark returns the logo at the closest available size that is not smaller
// than size (or the largest available one).
func Mark(size int) image.Image {
	chosen := markSizes[len(markSizes)-1]
	for _, s := range markSizes {
		if s >= size {
			chosen = s
			break
		}
	}
	if cached, ok := cache.Load(chosen); ok {
		return cached.(*image.RGBA)
	}
	data, err := marks.ReadFile(fmt.Sprintf("mark-%d.png", chosen))
	if err != nil {
		return image.NewRGBA(image.Rect(0, 0, chosen, chosen))
	}
	decoded, err := png.Decode(bytes.NewReader(data))
	if err != nil {
		return image.NewRGBA(image.Rect(0, 0, chosen, chosen))
	}
	rgba := image.NewRGBA(decoded.Bounds())
	draw.Draw(rgba, rgba.Bounds(), decoded, decoded.Bounds().Min, draw.Src)
	cache.Store(chosen, rgba)
	return rgba
}

// Status is the health shown on the tray icon.
type Status int

const (
	StatusOK      Status = iota // no badge: a healthy agent keeps the tray quiet
	StatusWarning               // amber: pending approval, connecting, spooling
	StatusError                 // red: unreachable, unauthorized, degraded
	StatusStopped               // grey: the agent is not running
)

var statusColors = map[Status]color.RGBA{
	StatusWarning: {R: 245, G: 158, B: 11, A: 255},
	StatusError:   {R: 220, G: 38, B: 38, A: 255},
	StatusStopped: {R: 107, G: 114, B: 128, A: 255},
}

// TrayImage returns the logo with a status badge in the lower-right corner.
func TrayImage(size int, status Status) image.Image {
	base := Mark(size)
	out := image.NewRGBA(image.Rect(0, 0, size, size))
	draw.Draw(out, out.Bounds(), base, base.Bounds().Min, draw.Src)
	fill, ok := statusColors[status]
	if !ok {
		return out
	}
	diameter := math.Max(6, float64(size)*0.5)
	cx := float64(size) - diameter/2
	cy := float64(size) - diameter/2
	ring := math.Max(1, float64(size)/16)
	// A white ring separates the badge from the mark on light and dark
	// taskbars alike.
	drawDisc(out, cx, cy, diameter/2, color.RGBA{R: 255, G: 255, B: 255, A: 255})
	drawDisc(out, cx, cy, diameter/2-ring, fill)
	return out
}

// drawDisc paints an anti-aliased filled circle.
func drawDisc(img *image.RGBA, cx, cy, radius float64, c color.RGBA) {
	b := img.Bounds()
	for y := b.Min.Y; y < b.Max.Y; y++ {
		for x := b.Min.X; x < b.Max.X; x++ {
			// 4x4 supersampling for a smooth edge.
			covered := 0
			for sy := 0; sy < 4; sy++ {
				for sx := 0; sx < 4; sx++ {
					dx := float64(x) + (float64(sx)+0.5)/4 - cx
					dy := float64(y) + (float64(sy)+0.5)/4 - cy
					if dx*dx+dy*dy <= radius*radius {
						covered++
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

func blend(under, over uint8, alpha float64) uint8 {
	return uint8(math.Round(float64(under)*(1-alpha) + float64(over)*alpha))
}

// Sizes lists the pre-rendered mark sizes, smallest first.
func Sizes() []int {
	out := append([]int(nil), markSizes...)
	sort.Ints(out)
	return out
}
