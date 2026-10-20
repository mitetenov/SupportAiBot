package bot

import (
	"context"
	"supportbot/internal/diagnostics"

	"sync"
	"time"
)

type MaintenanceJob struct {
	Name     string
	Interval time.Duration
	Run      func(context.Context) error
}
type Maintenance struct {
	cancel context.CancelFunc
	wg     sync.WaitGroup
}

func StartMaintenance(ctx context.Context, jobs []MaintenanceJob) *Maintenance {
	work, cancel := context.WithCancel(ctx)
	m := &Maintenance{cancel: cancel}
	for _, job := range jobs {
		j := job
		m.wg.Add(1)
		go func() {
			defer m.wg.Done()
			for {
				timer := time.NewTimer(j.Interval)
				select {
				case <-work.Done():
					timer.Stop()
					return
				case <-timer.C:
				}
				if e := j.Run(work); e != nil {
					diagnostics.Error(work, "Maintenance job failed", e, "job", j.Name)
				}
				diagnostics.Payload(work, "Maintenance tick completed", map[string]any{"job": j.Name})
			}
		}()
	}
	return m
}
func (m *Maintenance) Stop() { m.cancel(); m.wg.Wait() }
