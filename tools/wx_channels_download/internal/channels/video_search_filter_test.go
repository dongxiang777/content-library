package channels

import (
	"testing"
	"time"

	"wx_channel/internal/api/types"
)

func TestApplyVideoSearchLocalFilters_TimeAndSort(t *testing.T) {
	now := int(time.Now().Unix())
	old := now - 10*86400
	mid := now - 2*86400
	fresh := now - 3600
	r := &types.ChannelsVideoSearchResp{}
	r.Data.ObjectList = []types.ChannelsObject{
		{ID: "old", CreateTime: types.FlexInt(old)},
		{ID: "mid", CreateTime: types.FlexInt(mid)},
		{ID: "fresh", CreateTime: types.FlexInt(fresh)},
	}
	sort := 1
	tr := 1 // one day
	body := types.ChannelsVideoSearchBody{Sort: &sort, TimeRange: &tr}
	applyVideoSearchLocalFilters(r, body)
	if len(r.Data.ObjectList) != 1 {
		t.Fatalf("want 1 after day filter, got %d", len(r.Data.ObjectList))
	}
	if r.Data.ObjectList[0].ID != "fresh" {
		t.Fatalf("want fresh first, got %s", r.Data.ObjectList[0].ID)
	}

	// week keeps mid+fresh, sorted latest first
	r.Data.ObjectList = []types.ChannelsObject{
		{ID: "old", CreateTime: types.FlexInt(old)},
		{ID: "mid", CreateTime: types.FlexInt(mid)},
		{ID: "fresh", CreateTime: types.FlexInt(fresh)},
	}
	tr = 2
	body.TimeRange = &tr
	applyVideoSearchLocalFilters(r, body)
	if len(r.Data.ObjectList) != 2 {
		t.Fatalf("want 2 after week filter, got %d ids=%v", len(r.Data.ObjectList), ids(r))
	}
	if r.Data.ObjectList[0].ID != "fresh" || r.Data.ObjectList[1].ID != "mid" {
		t.Fatalf("bad order: %v", ids(r))
	}
}

func ids(r *types.ChannelsVideoSearchResp) []string {
	out := make([]string, 0, len(r.Data.ObjectList))
	for _, o := range r.Data.ObjectList {
		out = append(out, o.ID)
	}
	return out
}
