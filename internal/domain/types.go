// Package domain contains data shared by the Go bot's services.
package domain

import (
	"context"
	"time"
)

type Message struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}
type FAQEntry struct {
	Question string `json:"question"`
	Answer   string `json:"answer"`
	Keywords string `json:"keywords,omitempty"`
	Image    string `json:"image,omitempty"`
}
type FAQResult struct {
	Question   string  `json:"question"`
	Answer     string  `json:"answer"`
	Similarity float64 `json:"similarity"`
	RRFScore   float64 `json:"rrf_score"`
	Image      string  `json:"image,omitempty"`
}
type FAQContext struct {
	Text          string      `json:"text"`
	Results       []FAQResult `json:"results"`
	MaxSimilarity float64     `json:"max_similarity"`
	BestQuestion  string      `json:"best_question,omitempty"`
}

func (f FAQContext) Empty() bool { return len(f.Results) == 0 }
func (f FAQContext) Questions() map[string]bool {
	out := map[string]bool{}
	for _, r := range f.Results {
		out[r.Question] = true
	}
	return out
}

type Reply struct {
	Text       string
	FAQContext FAQContext
}
type TokenUsage struct {
	PromptTokens     int64
	CompletionTokens int64
	TotalTokens      int64
}
type LLM interface {
	Chat(context.Context, string, int64) (Reply, error)
	ChatWithImage(context.Context, string, int64, string, string) (Reply, error)
	ChatWithPDF(context.Context, string, int64, string) (Reply, error)
	SupportsImages() bool
	SupportsPDF() bool
}
type History interface {
	GetHistory(context.Context, int64) ([]Message, error)
	AddUserMessage(context.Context, int64, string) error
	AddAssistantMessage(context.Context, int64, string) error
	GetLastUserMessage(int64) string
	GetRejectedFAQQuestions(int64) map[string]bool
	RecordFAQContext(int64, FAQContext, []string)
	RejectLastFAQ(int64)
	ClearRejectedFAQsIfNewTopic(int64, string)
	Clear(context.Context, int64) error
	EvictStaleEntries(context.Context) error
}
type FAQ interface {
	BuildFAQContext(context.Context, string, map[string]bool) (FAQContext, error)
}
type UsageStore interface {
	SaveUsage(context.Context, int64, TokenUsage) error
}
type Gap struct {
	ID              int64
	UserQuery       string
	BestFAQQuestion string
	MaxSimilarity   float64
	FAQCount        int
	TriggerReason   string
	BotResponse     string
	GapCount        int64
	FirstSeen       time.Time
	LastSeen        time.Time
	TelegramID      *int64
}
type User struct {
	TelegramID int64
	Username   string
	FirstName  string
	LastName   string
}
type Topic struct {
	UserID         int64
	TopicID        int64
	UserName       string
	ActiveTicketID *int64
	CreatedAt      time.Time
}
type MessageMapping struct {
	ID             int64
	TopicMessageID int64
	TopicID        int64
	UserChatID     int64
	UserMessageID  int64
	CreatedAt      time.Time
}
type TicketProgress struct {
	LastAnsweredMessageID      int64
	LastBotReplyMessageID      int64
	LastHumanReplyMessageID    int64
	LastMirroredMediaMessageID int64
	PendingMediaMessageID      int64
	LastHumanReplyAt           *time.Time
}

func (p TicketProgress) HumanRecentlyActive(now time.Time, window time.Duration) bool {
	return p.LastHumanReplyAt != nil && now.Sub(*p.LastHumanReplyAt) < window
}
func (p TicketProgress) HumanReplyIsNew(id int64) bool { return id > p.LastHumanReplyMessageID }

type UserUsage struct {
	TelegramID       int64
	PromptTokens     int64
	CompletionTokens int64
	TotalTokens      int64
	RequestCount     int64
}
type Stats struct {
	Requests         int64
	Users            int64
	PromptTokens     int64
	CompletionTokens int64
	TotalTokens      int64
}
type Store interface {
	UpsertUser(context.Context, User) error
	UserByID(context.Context, int64) (*User, error)
	UsageTop(context.Context, int) ([]UserUsage, error)
	UsageForUser(context.Context, int64) (*UserUsage, error)
	TopicByUser(context.Context, int64) (*Topic, error)
	TopicByID(context.Context, int64) (*Topic, error)
	SaveTopic(context.Context, Topic) error
	DeleteTopic(context.Context, int64, int64) error
	SetActiveTicket(context.Context, int64, *int64) error
	SaveMessageMapping(context.Context, MessageMapping) error
	MappingByTopicMessage(context.Context, int64, int64) (*MessageMapping, error)
	MappingByUserMessage(context.Context, int64, int64) (*MessageMapping, error)
	TicketProgress(context.Context, int64) (TicketProgress, error)
	PendingMediaTicketIDs(context.Context, int) ([]int64, error)
	RecordPendingMedia(context.Context, int64, int64) error
	RecordMirroredMedia(context.Context, int64, int64) error
	RecordHumanReply(context.Context, int64, int64, time.Time) error
	RecordTicketReply(context.Context, int64, *int64, *int64) error
	Stats(context.Context) (Stats, error)
	ClearUsage(context.Context) (int64, error)
	PurgeMessageMappings(context.Context, time.Time) (int64, error)
}

type Gaps interface {
	Evaluate(context.Context, string, int64, string, FAQContext) error
	EvaluateOperatorRequest(context.Context, string, int64, FAQContext) error
	Top(context.Context, int) ([]Gap, error)
	Clear(context.Context) (int64, error)
}
