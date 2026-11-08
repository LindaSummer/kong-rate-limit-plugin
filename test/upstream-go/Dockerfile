FROM golang:1.27.2-alpine AS build
ENV CGO_ENABLED=0 GOTOOLCHAIN=local
WORKDIR /src
COPY go.mod main.go ./
RUN go build -trimpath -ldflags="-s -w" -o /server .

FROM scratch
COPY --from=build /server /server
USER 65532:65532
EXPOSE 8080
HEALTHCHECK --interval=2s --timeout=2s --start-period=1s --retries=15 CMD ["/server", "health"]
ENTRYPOINT ["/server"]
