# Multi-stage build: compile a static Go binary, ship it in a FROM scratch
# image - no shell, no interpreter, no libc, no package manager. The runtime
# attack surface is the binary and nothing else, which is also why there is
# no HEALTHCHECK here: Kubernetes' liveness/readiness probes (deployment.yaml)
# do that job over HTTP, and scratch has no shell to run one anyway.
FROM golang:1.26-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY main.go index.html ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /k3s-demo .

FROM scratch
COPY --from=build /k3s-demo /k3s-demo
ENV APP_VERSION=dev
USER 10001
EXPOSE 8000
ENTRYPOINT ["/k3s-demo"]
