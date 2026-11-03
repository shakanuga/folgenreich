import io

from conftest import STATE, show
from folgenreich import db, logic, sync

HEAD = "media_id,source,media_type,title,image,season_number,episode_number,score,status,notes,start_date,end_date,progress,created_at,progressed_at\n"
T = "2024-05-12 22:00:00+00:00"
SMALL = HEAD + f"""5,tmdb,tv,Beispiel,https://x/p.jpg,,,8.0,Completed,,{T},{T},3,{T},{T}
5,tmdb,season,Beispiel,https://x/s.jpg,1,,9.0,Completed,,{T},{T},3,{T},{T}
5,tmdb,episode,Beispiel,https://x/e.jpg,1,1,,,,,{T},,{T},
5,tmdb,episode,Beispiel,https://x/e.jpg,1,2,,,,,{T},,{T},
5,tmdb,episode,Beispiel,https://x/e.jpg,1,2,,,,,2024-06-01 22:00:00+00:00,,{T},
6,tmdb,tv,Abbruch,none,,,3.0,Dropped,,{T},{T},0,{T},{T}
7,mal,anime,Etwas,https://x,,,,Completed,,,,0,{T},
"""


def post(client, text):
    return client.post("/konto/import", data={"file": (io.BytesIO(text.encode()), "y.csv")},
                       content_type="multipart/form-data", follow_redirects=True)


def test_small_import_and_repeat(client):
    page = post(client, SMALL).get_data(as_text=True)
    assert "2 Serien" in page and "1× anime" in page
    post(client, SMALL)
    c = db.get()
    assert c.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 2
    assert c.execute("SELECT COUNT(*) FROM watches").fetchone()[0] == 2
    assert c.execute("SELECT plays FROM watches WHERE number=2").fetchone()[0] == 2
    assert c.execute("SELECT rating,notes FROM entries WHERE state='follow'").fetchone()[1] == "Bewertung aus Yamtrack: Staffel 1: 9/10"


def test_before_first_sync_yamtrack_status_counts(client):
    post(client, SMALL)
    items = {i["title"]: i for i in logic.tv_items(1)}
    assert items["Beispiel"]["tab"] == "archiv"
    assert items["Abbruch"]["sub"] == "dropped"


def test_completed_show_with_new_season_becomes_active_after_sync(client):
    post(client, SMALL)
    STATE["tv"][5] = show("Beispiel", seasons={1: ["2020-01-01", "2020-01-02"], 2: ["2026-01-01"]})
    STATE["tv"][6] = show("Abbruch", seasons={1: ["2020-01-01"]})
    assert sync.run_once() == 2
    items = {i["title"]: i for i in logic.tv_items(1)}
    assert items["Beispiel"]["tab"] == "aktiv" and logic.pos_label(items["Beispiel"]["next_pos"]) == "S02E01"
    assert items["Abbruch"]["tab"] == "archiv"


def test_rejects_other_files(client):
    assert "Yamtrack-Export" in post(client, "a,b\n1,2\n").get_data(as_text=True)
