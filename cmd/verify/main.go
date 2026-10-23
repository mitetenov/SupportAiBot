// verify measures Docker build-cache behavior without starting the application.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"supportbot/internal/verify"
	"syscall"
)

func run(ctx context.Context, args []string) error {
	if len(args) == 0 {
		return fmt.Errorf("usage: verify cache [flags]")
	}
	fs := flag.NewFlagSet(args[0], flag.ContinueOnError)
	image := fs.String("image", "supportbot-go:complete", "image under test")
	dir := fs.String("results", "", "cache results directory")
	if e := fs.Parse(args[1:]); e != nil {
		return e
	}
	var result any
	var e error
	switch args[0] {
	case "cache":
		result, e = verify.Cache(ctx, *image, *dir)
	default:
		return fmt.Errorf("unknown verification command")
	}
	if e != nil {
		return e
	}
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	return enc.Encode(result)
}
func main() {
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	if e := run(ctx, os.Args[1:]); e != nil {
		fmt.Fprintln(os.Stderr, e)
		os.Exit(1)
	}
}
