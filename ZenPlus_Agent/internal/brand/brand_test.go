package brand

import (
	"image/png"
	"os"
	"path/filepath"
	"testing"
)

func TestMarkSizesAndTrayBadges(t *testing.T) {
	for _, size := range []int{16, 20, 24, 32, 48, 64} {
		if got := Mark(size).Bounds().Dx(); got != size {
			t.Fatalf("Mark(%d) is %d px", size, got)
		}
	}
	ok := TrayImage(32, StatusOK)
	bad := TrayImage(32, StatusError)
	r, g, b, _ := bad.At(26, 26).RGBA()
	if r>>8 < 180 || g>>8 > 80 || b>>8 > 80 {
		t.Fatalf("error badge is not red: %d %d %d", r>>8, g>>8, b>>8)
	}
	r2, g2, b2, _ := ok.At(26, 26).RGBA()
	if r2 == r && g2 == g && b2 == b {
		t.Fatal("healthy tray icon unexpectedly carries the error badge")
	}
	if dir := os.Getenv("ZENPLUS_BRAND_PREVIEW"); dir != "" {
		for name, status := range map[string]Status{"ok": StatusOK, "warn": StatusWarning, "error": StatusError, "stopped": StatusStopped} {
			f, err := os.Create(filepath.Join(dir, "tray-"+name+".png"))
			if err != nil {
				t.Fatal(err)
			}
			_ = png.Encode(f, TrayImage(64, status))
			f.Close()
		}
	}
}

func TestStatusGlyphs(t *testing.T) {
	for _, g := range []Glyph{GlyphCheck, GlyphAlert, GlyphCross, GlyphPending, GlyphStopped} {
		img := StatusGlyph(48, g)
		if img.Bounds().Dx() != 48 {
			t.Fatalf("glyph %d is %d px", g, img.Bounds().Dx())
		}
		// The corner stays transparent; the rim carries the status colour.
		if _, _, _, a := img.At(0, 0).RGBA(); a != 0 {
			t.Fatalf("glyph %d corner is not transparent", g)
		}
		want := glyphColors[g]
		r, gg, b, _ := img.At(24, 3).RGBA()
		if uint8(r>>8) != want.R || uint8(gg>>8) != want.G || uint8(b>>8) != want.B {
			t.Fatalf("glyph %d rim colour = %d,%d,%d", g, r>>8, gg>>8, b>>8)
		}
	}
	if dir := os.Getenv("ZENPLUS_BRAND_PREVIEW"); dir != "" {
		for name, g := range map[string]Glyph{"check": GlyphCheck, "alert": GlyphAlert, "cross": GlyphCross, "pending": GlyphPending, "stopped": GlyphStopped} {
			f, err := os.Create(filepath.Join(dir, "glyph-"+name+".png"))
			if err != nil {
				t.Fatal(err)
			}
			_ = png.Encode(f, StatusGlyph(96, g))
			f.Close()
		}
	}
}
