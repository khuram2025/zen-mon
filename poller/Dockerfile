FROM golang:1.22-alpine AS builder

RUN apk add --no-cache git

WORKDIR /app
COPY go.mod go.sum ./
RUN go mod download

COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -o /zenplus-poller ./cmd/poller

FROM alpine:3.19

RUN apk add --no-cache ca-certificates tzdata

COPY --from=builder /zenplus-poller /usr/local/bin/zenplus-poller

# Health check port
EXPOSE 8081

ENTRYPOINT ["zenplus-poller"]
