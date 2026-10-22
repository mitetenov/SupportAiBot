package eval

import (
	"encoding/json"
	"os"
	"reflect"
	"testing"
)

func TestFrozenScorerContracts(t *testing.T) {
	raw, e := os.ReadFile("testdata/scorer_baseline.json")
	if e != nil {
		t.Fatal(e)
	}
	var cases []struct {
		Case     BehaviorCase `json:"case"`
		Response string       `json:"response"`
		Calls    []Call       `json:"calls"`
		Expected Result       `json:"expected"`
	}
	if e = json.Unmarshal(raw, &cases); e != nil {
		t.Fatal(e)
	}
	if len(cases) < 140 {
		t.Fatal("missing scorer contracts")
	}
	for i, c := range cases {
		t.Run(c.Case.Name+"/"+string(rune(i+65)), func(t *testing.T) {
			got := Score(c.Case, c.Response, c.Calls)
			if !reflect.DeepEqual(got, c.Expected) {
				t.Errorf("got %#v\nwant %#v", got, c.Expected)
			}
		})
	}
}
