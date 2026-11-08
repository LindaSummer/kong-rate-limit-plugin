package main

import (
	"bytes"
	"log"
	"net/http"
	"os"
	"time"
)

func main() {
	if len(os.Args) == 2 && os.Args[1] == "health" {
		client := &http.Client{Timeout: time.Second}
		response, err := client.Get("http://127.0.0.1:8080/")
		if err != nil {
			os.Exit(1)
		}
		response.Body.Close()
		if response.StatusCode != http.StatusOK || response.ContentLength != 512 {
			os.Exit(1)
		}
		return
	}

	body := []byte(`{"ok":true}`)
	body = append(body, bytes.Repeat([]byte(" "), 512-len(body))...)
	server := &http.Server{
		Addr:              ":8080",
		ReadHeaderTimeout: 5 * time.Second,
		Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "application/json")
			w.Header().Set("Content-Length", "512")
			if r.Method != http.MethodHead {
				_, _ = w.Write(body)
			}
		}),
	}
	log.Fatal(server.ListenAndServe())
}
