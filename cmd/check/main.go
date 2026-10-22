// check prepares an isolated pgvector test database and runs the Go race suite.
package main

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"supportbot/internal/storage"
)

func run() error {
	dsn := os.Getenv("TEST_DATABASE_URL")
	if dsn == "" {
		return fmt.Errorf("set TEST_DATABASE_URL to a disposable PostgreSQL/pgvector database")
	}
	store, err := storage.New(context.Background(), dsn)
	if err != nil {
		return fmt.Errorf("test database unavailable")
	}
	_, err = store.Pool.Exec(context.Background(), "CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
	store.Close()
	if err != nil {
		return fmt.Errorf("cannot initialize test pgvector extension")
	}
	args := append([]string{"test", "-race", "-count=1"}, os.Args[1:]...)
	args = append(args, "./...")
	command := exec.Command("go", args...)
	command.Stdin = os.Stdin
	command.Stdout = os.Stdout
	command.Stderr = os.Stderr
	return command.Run()
}
func main() {
	if e := run(); e != nil {
		fmt.Fprintln(os.Stderr, e)
		os.Exit(1)
	}
}
