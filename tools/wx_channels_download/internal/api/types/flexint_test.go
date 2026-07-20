package types

import (
	"encoding/json"
	"testing"
)

func TestFlexIntUnmarshal(t *testing.T) {
	cases := []struct {
		name string
		raw  string
		want int
	}{
		{"number", `12345`, 12345},
		{"string", `"12345"`, 12345},
		{"empty_string", `""`, 0},
		{"null", `null`, 0},
		{"float_number", `123.0`, 123},
		{"float_string", `"123.0"`, 123},
		{"zero", `0`, 0},
		{"zero_string", `"0"`, 0},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var f FlexInt
			if err := json.Unmarshal([]byte(tc.raw), &f); err != nil {
				t.Fatalf("unmarshal %s: %v", tc.raw, err)
			}
			if f.Int() != tc.want {
				t.Fatalf("got %d, want %d", f.Int(), tc.want)
			}
		})
	}
}

func TestChannelsMediaItemFileSizeString(t *testing.T) {
	raw := `{
		"url": "https://example.com/v",
		"mediaType": 4,
		"videoPlayLen": "7",
		"width": 1080,
		"height": 1920,
		"fileSize": "11557355",
		"spec": [{"fileFormat": "xWT111", "width": 720, "height": 1280, "durationMs": "7233"}],
		"coverUrl": "https://example.com/c",
		"decodeKey": "930220499",
		"urlToken": "&token=x"
	}`
	var media ChannelsMediaItem
	if err := json.Unmarshal([]byte(raw), &media); err != nil {
		t.Fatalf("unmarshal media: %v", err)
	}
	if media.FileSize.Int() != 11557355 {
		t.Fatalf("fileSize got %d", media.FileSize.Int())
	}
	if media.VideoPlayLen.Int() != 7 {
		t.Fatalf("videoPlayLen got %d", media.VideoPlayLen.Int())
	}
	if len(media.Spec) != 1 || media.Spec[0].DurationMs.Int() != 7233 {
		t.Fatalf("durationMs got %+v", media.Spec)
	}
}

func TestChannelsObjectCreateTimeString(t *testing.T) {
	raw := `{"id":"1","objectDesc":{"description":"","media":null,"mediaType":0},"objectNonceId":"n","createtime":"1733911369"}`
	var obj ChannelsObject
	if err := json.Unmarshal([]byte(raw), &obj); err != nil {
		t.Fatalf("unmarshal object: %v", err)
	}
	if obj.CreateTime.Int() != 1733911369 {
		t.Fatalf("createtime got %d", obj.CreateTime.Int())
	}
}

func TestFlexIntMarshal(t *testing.T) {
	f := FlexInt(42)
	b, err := json.Marshal(f)
	if err != nil {
		t.Fatal(err)
	}
	if string(b) != "42" {
		t.Fatalf("marshal got %s", b)
	}
}

// Reproduces the live search failure:
// json: cannot unmarshal string into Go struct field ChannelsMediaItem...fileSize of type int
func TestChannelsContactSearchRespFileSizeString(t *testing.T) {
	raw := `{
		"errCode": 0,
		"errMsg": "ok",
		"data": {
			"BaseResponse": {"Ret": 0, "ErrMsg": {"String": ""}},
			"infoList": [],
			"continueFlag": 0,
			"lastBuff": "",
			"topicInfoList": [],
			"musicInfoList": [],
			"multiFeedStream": [],
			"objectList": [{
				"id": "14545102784038246591",
				"contact": {
					"username": "v2_test@finder",
					"nickname": "test",
					"headUrl": "https://example.com/h",
					"signature": "",
					"coverImgUrl": ""
				},
				"objectDesc": {
					"description": "遗嘱相关",
					"media": [{
						"url": "https://finder.video.qq.com/v",
						"mediaType": 4,
						"videoPlayLen": 7,
						"width": 1080,
						"height": 1920,
						"fileSize": "11557355",
						"spec": [{"fileFormat": "xWT111", "width": 720, "height": 1280, "durationMs": 7233}],
						"coverUrl": "https://example.com/c",
						"decodeKey": "930220499",
						"urlToken": "&token=x"
					}],
					"mediaType": 4
				},
				"objectNonceId": "nonce",
				"source_url": "",
				"createtime": 1733911369
			}]
		},
		"payload": {"query": "遗嘱", "scene": 0, "lastBuff": "", "requestId": ""}
	}`
	var resp ChannelsContactSearchResp
	if err := json.Unmarshal([]byte(raw), &resp); err != nil {
		t.Fatalf("unmarshal search resp: %v", err)
	}
	if len(resp.Data.ObjectList) != 1 {
		t.Fatalf("objectList len=%d", len(resp.Data.ObjectList))
	}
	media := resp.Data.ObjectList[0].ObjectDesc.Media[0]
	if media.FileSize.Int() != 11557355 {
		t.Fatalf("fileSize=%d", media.FileSize.Int())
	}
	prof := ChannelsObjectToChannelsFeedProfile(&resp.Data.ObjectList[0])
	if prof == nil || prof.FileSize != 11557355 {
		t.Fatalf("profile conversion failed: %+v", prof)
	}
}
