FROM golang:1.23-alpine AS build

ARG TARGETOS=linux
ARG TARGETARCH=arm64

WORKDIR /src
COPY go.mod ./
COPY cmd ./cmd
RUN CGO_ENABLED=0 GOOS=$TARGETOS GOARCH=$TARGETARCH go build -o /out/motor-probe ./cmd/motor-probe

FROM scratch
COPY --from=build /out/motor-probe /motor-probe
CMD ["/motor-probe"]
