package sflow

import (
	"encoding/binary"
	"errors"
	"fmt"
	"net/netip"
)

// errTruncated is returned when a structure runs past the end of its buffer.
var errTruncated = errors.New("truncated")

// reader decodes the XDR (RFC 4506) encoding sFlow uses: big-endian 32-bit
// words, opaque data padded to a multiple of four bytes, arrays prefixed by
// their element count. Errors are sticky: after the first short read every
// read returns zero and err stays set, so a structure is decoded straight
// through and checked once, and no read can go past the buffer.
type reader struct {
	b   []byte
	err error
}

func (r *reader) fail(err error) {
	if r.err == nil {
		r.err = err
	}
	r.b = nil
}

func (r *reader) u32() uint32 {
	if len(r.b) < 4 {
		r.fail(errTruncated)
		return 0
	}
	v := binary.BigEndian.Uint32(r.b)
	r.b = r.b[4:]
	return v
}

func (r *reader) u64() uint64 {
	if len(r.b) < 8 {
		r.fail(errTruncated)
		return 0
	}
	v := binary.BigEndian.Uint64(r.b)
	r.b = r.b[8:]
	return v
}

// fixed returns the next n bytes and skips their padding. Missing padding at
// the very end of a buffer is tolerated (the value itself is complete).
func (r *reader) fixed(n int) []byte {
	if n < 0 || len(r.b) < n {
		r.fail(errTruncated)
		return nil
	}
	v := r.b[:n:n]
	r.b = r.b[n:]
	if pad := (4 - n%4) % 4; pad > 0 {
		r.b = r.b[min(pad, len(r.b)):]
	}
	return v
}

// opaque reads variable-length opaque data (length word + bytes + padding).
func (r *reader) opaque() []byte {
	n := r.u32()
	if r.err != nil {
		return nil
	}
	if uint64(n) > uint64(len(r.b)) {
		r.fail(errTruncated)
		return nil
	}
	return r.fixed(int(n))
}

// count reads an array length and checks that the elements can fit in what
// is left (each needs at least minSize bytes), so a corrupt count can never
// drive a huge loop or allocation.
func (r *reader) count(minSize int) int {
	n := r.u32()
	if r.err != nil {
		return 0
	}
	if uint64(n)*uint64(minSize) > uint64(len(r.b)) {
		r.fail(fmt.Errorf("array of %d elements exceeds the %d bytes left", n, len(r.b)))
		return 0
	}
	return int(n)
}

// Address types of the sFlow "address" union.
const (
	addrUnknown = 0
	addrIPv4    = 1
	addrIPv6    = 2
)

// addr reads an sFlow address: a type word, then 4 (IPv4) or 16 (IPv6)
// bytes; type 0 (unknown) carries no bytes and yields the zero Addr.
func (r *reader) addr() netip.Addr {
	switch t := r.u32(); {
	case r.err != nil:
		return netip.Addr{}
	case t == addrIPv4:
		if b := r.fixed(4); b != nil {
			return netip.AddrFrom4([4]byte(b))
		}
	case t == addrIPv6:
		if b := r.fixed(16); b != nil {
			return netip.AddrFrom16([16]byte(b))
		}
	case t == addrUnknown:
	default:
		r.fail(fmt.Errorf("unknown address type %d", t))
	}
	return netip.Addr{}
}
