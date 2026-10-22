// nextversion writes the next unused Docker Hub version to GITHUB_OUTPUT/stdout.
package main

import (
	"context"
	"fmt"
	"net/http"
	"os"
	"supportbot/internal/release"
	"time"
)

func run(args []string) error {
	if len(args) != 1 {
		return fmt.Errorf("usage: nextversion <docker-hub-repository>")
	}
	major, err := release.Major("VERSION")
	if err != nil {
		return err
	}
	client := &http.Client{Timeout: 30 * time.Second, CheckRedirect: func(_ *http.Request, _ []*http.Request) error { return http.ErrUseLastResponse }}
	tags, err := release.Tags(context.Background(), client, "https://hub.docker.com", args[0])
	if err != nil {
		return err
	}
	version, err := release.Next(major, tags)
	if err != nil {
		return err
	}
	fmt.Fprintf(os.Stderr, "::notice::publishing %s:%s\n", args[0], version)
	fmt.Printf("version=%s\n", version)
	return nil
}
func main() {
	if err := run(os.Args[1:]); err != nil {
		fmt.Fprintln(os.Stderr, "::error::", err)
		os.Exit(1)
	}
}
