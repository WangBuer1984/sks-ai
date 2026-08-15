from app.datasource.tikhub import (
    VideoMeta,
    _duration_sec,
    _parse_channels_video,
    _parse_video,
    _safe_int,
    video_meta_to_media_ref,
)

def test_parse_video_extracts_author_nickname():
    item = {
        "desc": "标题",
        "statistics": {
            "play_count": 1,
            "digg_count": 2,
            "collect_count": 9,
            "comment_count": 3,
            "share_count": 4,
        },
        "video": {
            "duration": 21500,  # 毫秒 → 22 秒
            "play_addr": {"url_list": ["https://cdn.example/a.mp4"]},
        },
        "author": {"nickname": "张三"},
    }
    v = _parse_video(item)
    assert v.author == "张三"
    assert v.title == "标题"
    assert v.like_count == 2
    assert v.collect_count == 9
    assert v.fav_count == 9
    assert v.comment_count == 3
    assert v.share_count == 4
    assert v.duration_sec == 22


def test_duration_sec_ms_and_s():
    assert _duration_sec(15000, unit="ms") == 15
    assert _duration_sec(45, unit="s") == 45
    assert _duration_sec(570, unit="auto") == 570  # 视频号秒
    assert _duration_sec(21000, unit="auto") == 21  # ≥1000 当毫秒
    assert _duration_sec(0) is None
    assert _duration_sec(None) is None


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
    assert _safe_int("1.5m") == 1_500_000
    assert _safe_int(True) == 1
    assert _safe_int(False) == 0
    assert _safe_int("bad") == 0
    assert _safe_int(None) == 0


def test_parse_channels_video_formatted_read_count():
    v = _parse_channels_video({
        "title": "t",
        "read_count": "1,234",
        "fav_count": "2.5k",
        "like_count": "100",
        "forward_count": "7",
        "comment_count": "8",
        "create_time": 1711305600,
        "nickname": "a",
        "media": {
            "full_url": "http://cdn/v.mp4",
            "decode_key": "k1",
            "duration": 570,  # 秒
        },
    })
    assert v is not None
    assert v.play_count == 1234
    assert v.fav_count == 2500
    assert v.collect_count == 2500
    assert v.like_count == 100
    assert v.share_count == 7
    assert v.comment_count == 8
    assert v.published_at == 1711305600
    assert v.duration_sec == 570

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
