package verify

import (
	"testing"
)

func TestBuildKitStepParsing(t *testing.T) {
	got := Steps("#7 [builder 3/5] RUN go mod download\n#7 CACHED\n#8 [builder 4/5] RUN --mount=type=bind go build -mod=readonly -o /out/app/supportbot\n#8 DONE 2.0s\n#9 [runtime 1/3] COPY --link faq/ /app/faq/\n#9 CACHED\n#10 [runtime 2/3] COPY --link --from=builder /out/ /\n#10 CACHED\n#11 [runtime 3/3] COPY --link --from=runtime-files /rootfs/ /\n#11 CACHED")
	if len(got) != 5 || got["modules"] != "CACHED" || got["compile"] != "DONE" || got["binary"] != "CACHED" {
		t.Fatal(got)
	}
}

func TestRepeatedCacheProbesUseDistinctValidFAQInputs(t *testing.T) {
	source := []byte(`[{"question":"test","answer":"answer"}]`)
	first, e := probeFAQ(source, "first")
	if e != nil {
		t.Fatal(e)
	}
	second, e := probeFAQ(source, "second")
	if e != nil || string(first) == string(second) {
		t.Fatal("cache probes reused an input", e)
	}
}
