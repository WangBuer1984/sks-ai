from app.datasource.tikhub import (
    VideoMeta,
    _parse_channels_video,
    _parse_video,
    _safe_int,
    video_meta_to_media_ref,
)

def test_parse_video_extracts_author_nickname():
    item = {
        "desc": "标题",
        "statistics": {"play_count": 1, "digg_count": 2},
        "video": {"play_addr": {"url_list": ["https://cdn.example/a.mp4"]}},
        "author": {"nickname": "张三"},
    }
    v = _parse_video(item)
    assert v.author == "张三"
    assert v.title == "标题"


def test_parse_video_null_url_list_entry_becomes_empty():
    item = {
        "desc": "t",
        "statistics": {},
        "video": {"play_addr": {"url_list": [None]}},
        "author": {},
    }
    v = _parse_video(item)
    assert v.download_url == ""


def test_safe_int_tolerates_formatted_counts():
    assert _safe_int("1,234") == 1234
    assert _safe_int("1.2k") == 1200
    assert _safe_int("bad") == 0
    assert _safe_int(None) == 0


def test_parse_channels_video_formatted_read_count():
    v = _parse_channels_video({
        "title": "t",
        "read_count": "1,234",
        "fav_count": "2.5k",
        "nickname": "a",
        "media": {"full_url": "http://cdn/v.mp4", "decode_key": "k1"},
    })
    assert v is not None
    assert v.play_count == 1234
    assert v.fav_count == 2500

def test_video_meta_to_media_ref_fills_headers_title_author():
    v = VideoMeta(
        title="你好", play_count=1, fav_count=2,
        download_url="https://cdn.example/a.mp4", author="张三",
    )
    ref = video_meta_to_media_ref(v)
    assert ref.platform == "douyin"
    assert ref.author == "张三"
    assert ref.title == "你好"
    assert ref.headers["Referer"] == "https://www.douyin.com/"
    assert "User-Agent" in ref.headers
