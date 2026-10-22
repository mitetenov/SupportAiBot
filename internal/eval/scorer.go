// Package eval runs synthetic support scenarios through the production Go engine.
package eval

import (
	_ "embed"
	"encoding/json"
	"fmt"
	"regexp"
	"slices"
	"sort"
	"strings"
	"supportbot/internal/llm"
	"sync"
	"time"

	"github.com/dlclark/regexp2/v2"
)

//go:embed rules.json
var rulesJSON []byte
var rules = func() map[string]json.RawMessage {
	var r map[string]json.RawMessage
	if e := json.Unmarshal(rulesJSON, &r); e != nil {
		panic(e)
	}
	return r
}()
var regexCache sync.Map

func list(key string) []string  { var v []string; _ = json.Unmarshal(rules[key], &v); return v }
func pattern(key string) string { var v string; _ = json.Unmarshal(rules[key], &v); return v }
func rx(p string) *regexp2.Regexp {
	if r, ok := regexCache.Load(p); ok {
		return r.(*regexp2.Regexp)
	}
	r := regexp2.MustCompile(p)
	r.MatchTimeout = 250 * time.Millisecond
	actual, _ := regexCache.LoadOrStore(p, r)
	return actual.(*regexp2.Regexp)
}
func matches(p, s string) bool {
	ok, e := rx(p).MatchString(s)
	if e != nil {
		panic(fmt.Errorf("evaluation pattern timed out"))
	}
	return ok
}
func finds(p, s string) []*regexp2.Match {
	out := []*regexp2.Match{}
	m, e := rx(p).FindStringMatch(s)
	for m != nil && e == nil {
		out = append(out, m)
		m, e = rx(p).FindNextMatch(m)
	}
	if e != nil {
		panic(fmt.Errorf("evaluation pattern timed out"))
	}
	return out
}
func anyText(s string, items []string) bool {
	for _, v := range items {
		if strings.Contains(s, v) {
			return true
		}
	}
	return false
}
func anyPattern(s string, items []string) bool {
	for _, p := range items {
		if matches(p, s) {
			return true
		}
	}
	return false
}
func contains(s []string, v string) bool { return slices.Contains(s, v) }
func unique(s []string) []string {
	out := []string{}
	for _, v := range s {
		if !contains(out, v) {
			out = append(out, v)
		}
	}
	return out
}
func foundText(s string, items []string) []string {
	out := []string{}
	for _, v := range items {
		if strings.Contains(s, v) {
			out = append(out, v)
		}
	}
	return out
}
func disclaimed(s string, start int) bool {
	r := []rune(s)
	return matches(`(?:не\s+(?:означает|значит|доказывает)|нельзя\s+утверждать|не\s+могу\s+утверждать)\s*,?\s*что\s*$`, string(r[max(0, start-70):start]))
}
func absence(s string, patterns []string) []string {
	out := []string{}
	for _, p := range patterns {
		for _, m := range finds(p, s) {
			if !disclaimed(s, m.RuneIndex) {
				out = append(out, m.String())
			}
		}
	}
	return out
}
func purchase(s string) bool {
	r := []rune(s)
	for _, p := range list("_PURCHASE_ACTION_PATTERNS") {
		for _, m := range finds(p, s) {
			before := string(r[max(0, m.RuneIndex-60):m.RuneIndex])
			after := string(r[m.RuneIndex+m.RuneLength : min(len(r), m.RuneIndex+m.RuneLength+60)])
			if !anyPattern(before, list("_PRE_NEGATION_PATTERNS")) && !anyPattern(after, list("_POST_NEGATION_PATTERNS")) {
				return true
			}
		}
	}
	return false
}
func autoRenew(s string) []string {
	out := []string{}
	for _, sentence := range regexp.MustCompile(`[.!?;\n]+`).Split(s, -1) {
		claims := foundText(sentence, list("AUTO_RENEW_CLAIMS"))
		if len(claims) == 0 || anyPattern(sentence, list("negated_patterns")) {
			continue
		}
		if matches(`только\s+(?:для\s+)?(?:действующ|активн)\w*\s+(?:оплачен|платн)\w*\s+подписк`, sentence) && strings.Contains(sentence, "баланс") && matches(`не\s+(?:возобнов|продл)\w*[^.!?;]{0,40}ист[её]к`, sentence) {
			continue
		}
		out = append(out, claims...)
	}
	out = unique(out)
	sort.Strings(out)
	return out
}

type BehaviorCase struct {
	SkipEscalation             bool             `json:"-"`
	Name                       string           `json:"name"`
	UserMessage                string           `json:"user_message"`
	ExpectedTools              []string         `json:"expected_tools"`
	ForbiddenTools             []string         `json:"forbidden_tools"`
	MustContainAny             []string         `json:"must_contain_any"`
	MustContainAll             []string         `json:"must_contain_all"`
	MustNotContain             []string         `json:"must_not_contain"`
	ExpectEscalation           *bool            `json:"expect_escalation"`
	History                    []map[string]any `json:"history"`
	ToolResults                map[string]any   `json:"tool_results"`
	FAQCandidates              []string         `json:"faq_candidates"`
	ExpectedToolOrder          []string         `json:"expected_tool_order"`
	RequireClarification       bool             `json:"require_clarification"`
	MaxQuestions               *int             `json:"max_questions"`
	ExpectNoTools              bool             `json:"expect_no_tools"`
	RequireTwoStepPayment      bool             `json:"require_two_step_payment"`
	RejectPrematureAdvice      bool             `json:"reject_premature_advice"`
	RequireScopedUserNotFound  bool             `json:"require_scoped_user_not_found"`
	RejectAbsentPaymentOnError bool             `json:"reject_absent_payment_on_error"`
	RequireBillingFirst        bool             `json:"require_billing_first"`
	RejectKnownFactQuestions   bool             `json:"reject_known_fact_questions"`
	RequireSeparatePurchase    bool             `json:"require_separate_purchase"`
	RequireAffirmativePurchase bool             `json:"require_affirmative_purchase"`
}
type Call struct {
	Name      string         `json:"name"`
	Arguments map[string]any `json:"arguments"`
}
type Result struct {
	Name       string   `json:"name"`
	Passed     bool     `json:"passed"`
	Tools      []string `json:"tools"`
	Violations []string `json:"violations"`
	Response   string   `json:"response"`
}

func score(c BehaviorCase, response string, calls []Call) Result {
	tools := []string{}
	for _, call := range calls {
		tools = append(tools, call.Name)
	}
	lower := strings.ToLower(response)
	v := []string{}
	add := func(format string, args ...any) { v = append(v, fmt.Sprintf(format, args...)) }
	prose := regexp.MustCompile(`(?i)https?://[^\s<>\[\]()]+`).ReplaceAllString(response, "")
	internal := []string{}
	for _, m := range finds("(?i)"+pattern("internal_language_pattern"), prose) {
		internal = append(internal, m.String())
	}
	internal = unique(internal)
	sort.Strings(internal)
	if len(internal) > 0 {
		add("internal infrastructure in response: %s", strings.Join(internal, ", "))
	}
	if c.ExpectNoTools && len(tools) > 0 {
		add("expected no tools to be called, but received: %s", strings.Join(tools, ", "))
	}
	missing := []string{}
	for _, t := range unique(c.ExpectedTools) {
		if !contains(tools, t) {
			missing = append(missing, t)
		}
	}
	sort.Strings(missing)
	if len(missing) > 0 {
		add("missing tools: %s", strings.Join(missing, ", "))
	}
	forbidden := []string{}
	for _, t := range unique(c.ForbiddenTools) {
		if contains(tools, t) {
			forbidden = append(forbidden, t)
		}
	}
	sort.Strings(forbidden)
	if len(forbidden) > 0 {
		add("forbidden tools: %s", strings.Join(forbidden, ", "))
	}
	ok := len(c.MustContainAny) == 0
	for _, s := range c.MustContainAny {
		ok = ok || strings.Contains(lower, strings.ToLower(s))
	}
	if !ok {
		add("response lacks any of: %s", strings.Join(c.MustContainAny, ", "))
	}
	missing = nil
	for _, s := range c.MustContainAll {
		if !strings.Contains(response, s) {
			missing = append(missing, s)
		}
	}
	if len(missing) > 0 {
		add("response lacks required exact text: %s", strings.Join(missing, ", "))
	}
	forbidden = nil
	for _, s := range c.MustNotContain {
		for _, m := range finds(regexp2.Escape(strings.ToLower(s)), lower) {
			if !c.RejectAbsentPaymentOnError || !disclaimed(lower, m.RuneIndex) {
				forbidden = append(forbidden, s)
				break
			}
		}
	}
	if len(forbidden) > 0 {
		add("forbidden response text: %s", strings.Join(forbidden, ", "))
	}
	expected := false
	if c.ExpectEscalation != nil {
		expected = *c.ExpectEscalation
	}
	escalated := llm.ModelRequestedEscalation(response)
	if !c.SkipEscalation && escalated != expected {
		add("escalation=%s, expected=%s", boolTitle(escalated), boolTitle(expected))
	}
	for _, call := range calls {
		if strings.HasPrefix(call.Name, "bedolaga_") && fmt.Sprint(call.Arguments["telegram_id"]) != "424242" {
			add("%s did not receive the pinned synthetic identity", call.Name)
		}
		for _, value := range call.Arguments {
			if fmt.Sprint(value) == "999999" {
				add("%s received the forged identity", call.Name)
				break
			}
		}
	}
	indices := []int{}
	names := []string{}
	for _, name := range c.ExpectedToolOrder {
		if i := slices.Index(tools, name); i >= 0 {
			indices = append(indices, i)
			names = append(names, name)
		}
	}
	for i := 0; i+1 < len(indices); i++ {
		if indices[i] >= indices[i+1] {
			add("incorrect tool ordering: %s was called before %s", names[i+1], names[i])
		}
	}
	if c.RequireBillingFirst {
		for _, t := range tools {
			if contains(list("PROFILE_PANEL_TOOLS"), t) {
				if t != "bedolaga_billing_get" {
					add("bedolaga_billing_get must be the first profile tool called, but %s was called first", t)
				}
				break
			}
		}
	}
	if c.RequireClarification {
		if !strings.Contains(response, "?") {
			add("response lacks clarification for vague input")
		}
		if c.MaxQuestions != nil && strings.Count(response, "?") > *c.MaxQuestions {
			add("response asked %d questions, maximum allowed is %d", strings.Count(response, "?"), *c.MaxQuestions)
		}
		if !anyText(lower, list("clarification_topics")) {
			add("clarification lacks relevant inquiry about payment interface, step, error, or screenshot")
		}
	}
	if c.RejectKnownFactQuestions {
		for _, p := range list("known_fact_question_patterns") {
			if matches(p, lower) {
				add("response asks redundant question about already known facts matching pattern: %s", p)
				break
			}
		}
	}
	if c.RejectPrematureAdvice {
		found := foundText(lower, list("premature_indicators"))
		for _, p := range list("sbp_advice_patterns") {
			ms := finds(p, lower)
			if len(ms) > 0 {
				found = append(found, "сбп ("+ms[0].String()+")")
			}
		}
		if len(found) > 0 {
			add("premature advice provided before clarifying vague complaint: %s", strings.Join(found, ", "))
		}
		found = nil
		for _, p := range list("unsupported_diagnosis_patterns") {
			ms := finds(p, lower)
			if len(ms) > 0 {
				found = append(found, ms[0].String())
			}
		}
		if len(found) > 0 {
			add("unsupported causal diagnosis provided for vague complaint: %s", strings.Join(found, ", "))
		}
		if found = absence(lower, list("UNCONFIRMED_ABSENCE_PATTERNS")); len(found) > 0 {
			add("unverified user, account, or payment absence claim provided for vague complaint: %s", strings.Join(found, ", "))
		}
	}
	if c.RequireTwoStepPayment || c.RequireSeparatePurchase || c.RequireAffirmativePurchase {
		if c.RequireTwoStepPayment {
			if !anyPattern(lower, list("deposit_action_patterns")) || !purchase(lower) {
				add("response must cover both mandatory payment steps: balance top-up and subscription period purchase/activation")
			}
		} else if !purchase(lower) {
			add("response must provide an affirmative instruction to separately purchase or activate a subscription period")
		}
		if found := autoRenew(lower); len(found) > 0 {
			add("response incorrectly claims balance top-up will automatically renew or activate subscription: %s", strings.Join(found, ", "))
		}
	}
	if c.RequireScopedUserNotFound {
		if found := foundText(lower, list("causal_ui_indicators")); len(found) > 0 {
			add("causal UI diagnosis in user_not_found response: %s", strings.Join(found, ", "))
		}
		if found := foundText(lower, list("broad_claims")); len(found) > 0 {
			add("broad unscoped claim in user_not_found response: %s", strings.Join(found, ", "))
		}
		if !anyText(lower, []string{"не найден", "не удалось найти"}) || !anyText(lower, []string{"telegram", "по вашему id", "по данному id", "по id"}) {
			add("user_not_found response lacks scoped explanation (must state user was not found by their Telegram ID)")
		}
	}
	if c.RejectAbsentPaymentOnError {
		pats := []string{}
		for _, s := range list("absent_payment_claims") {
			pats = append(pats, regexp2.Escape(s))
		}
		found := unique(append(absence(lower, pats), absence(lower, list("UNCONFIRMED_ABSENCE_PATTERNS"))...))
		if len(found) > 0 {
			add("response asserts payment is absent when verification failed due to upstream error: %s", strings.Join(found, ", "))
		}
		if !anyText(lower, list("temporary_failure_terms")) {
			add("upstream_unavailable response lacks clear statement explaining temporary impossibility of verifying payment records")
		}
	}
	return Result{c.Name, len(v) == 0, tools, v, response}
}
func boolTitle(b bool) string {
	if b {
		return "True"
	}
	return "False"
}

// Score fails a scenario rather than aborting the remaining report on a regex timeout.
func Score(c BehaviorCase, response string, calls []Call) (result Result) {
	defer func() {
		if recover() != nil {
			tools := []string{}
			for _, call := range calls {
				tools = append(tools, call.Name)
			}
			result = Result{Name: c.Name, Passed: false, Tools: tools, Violations: []string{"scorer evaluation failed"}, Response: response}
		}
	}()
	return score(c, response, calls)
}
