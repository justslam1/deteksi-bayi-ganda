import streamlit as st
import pandas as pd
import re
from difflib import SequenceMatcher
import io

# Konfigurasi Halaman Streamlit
st.set_page_config(
    page_title="Deteksi Data Bayi Ganda",
    page_icon="🩺",
    layout="wide"
)

# --- FUNGSI HELPER & LOGIKA DETEKSI ---

def clean_text(text):
    if pd.isna(text):
        return ""
    return re.sub(r'[^a-z0-9\s]', '', str(text).lower().strip())

def similarity_score(a, b):
    return SequenceMatcher(None, a, b).ratio()

def run_detection(df):
    df = df.copy()
    
    # Preprocessing
    df['nama_anak_clean'] = df['Nama Anak'].apply(clean_text)
    df['ortu_clean'] = df['Nama Orang Tua'].apply(clean_text)
    
    # Konversi Tanggal Lahir ke string ISO
    df['tgl_lahir_clean'] = pd.to_datetime(df['Tanggal Lahir Anak'], errors='coerce').dt.strftime('%Y-%m-%d')
    
    df['duplicate_tier'] = None
    df['group_id'] = None
    
    group_counter = 1
    processed = set()
    rows = df.to_dict('records')
    n = len(rows)
    
    for i in range(n):
        if i in processed:
            continue
        
        matches = [i]
        tier = None
        
        for j in range(i + 1, n):
            if j in processed:
                continue
                
            # --- TIER 1: Exact NIK Match (NIK Valid 16 Digit) ---
            nik_i = str(rows[i].get('NIK Anak', '')).replace("'", "").strip()
            nik_j = str(rows[j].get('NIK Anak', '')).replace("'", "").strip()
            if len(nik_i) == 16 and len(nik_j) == 16 and nik_i == nik_j:
                matches.append(j)
                tier = "Tier 1: NIK Valid Sama"
                continue
                
            # --- TIER 2: Nama Anak + Tanggal Lahir Sama Persis ---
            if (rows[i]['nama_anak_clean'] == rows[j]['nama_anak_clean'] and 
                rows[i]['tgl_lahir_clean'] == rows[j]['tgl_lahir_clean'] and 
                len(rows[i]['nama_anak_clean']) > 2):
                matches.append(j)
                tier = tier or "Tier 2: Nama & Tgl Lahir Sama"
                continue
                
            # --- TIER 3: Fuzzy Match / Ortu + Tanggal Lahir ---
            if rows[i]['tgl_lahir_clean'] and rows[i]['tgl_lahir_clean'] == rows[j]['tgl_lahir_clean']:
                # 3A. Tanggal lahir sama + Nama Ortu sama
                if rows[i]['ortu_clean'] and rows[i]['ortu_clean'] == rows[j]['ortu_clean']:
                    matches.append(j)
                    tier = tier or "Tier 3: Ortu & Tgl Lahir Sama"
                    continue
                
                # 3B. Tanggal lahir sama + Kemiripan Nama Anak >= 85%
                sim = similarity_score(rows[i]['nama_anak_clean'], rows[j]['nama_anak_clean'])
                if sim >= 0.85 and len(rows[i]['nama_anak_clean']) > 3:
                    matches.append(j)
                    tier = tier or f"Tier 3: Kemiripan Nama ({int(sim*100)}%)"
                    continue

        if len(matches) > 1:
            g_id = f"DUP-{group_counter:04d}"
            for idx in matches:
                df.at[idx, 'duplicate_tier'] = tier
                df.at[idx, 'group_id'] = g_id
                processed.add(idx)
            group_counter += 1
            
    # Hapus kolom pembantu
    df = df.drop(columns=['nama_anak_clean', 'ortu_clean', 'tgl_lahir_clean'])
    return df


# --- INTERFACE UTAMA STREAMLIT ---

st.title("💉Aplikasi Deteksi Data Bayi Ganda")
st.markdown("Sistem berbasis web untuk mendeteksi data kohort bayi ganda (Tier 1-3) serta **menggabungkan (merge) atau memverifikasi data** secara interaktif.")

# --- EXPANDER PENJELASAN TIER ---
with st.expander("ℹ️ **Penjelasan Kriteria Deteksi (Tier 1, Tier 2, & Tier 3)**"):
    col_t1, col_t2, col_t3 = st.columns(3)
    
    with col_t1:
        st.markdown("""
        #### 🟢 Tier 1: NIK Valid Sama
        * **Kriteria:** NIK Anak sama persis dan bernilai sah (16 digit angka).
        * **Kepastian:** **Sangat Tinggi**
        * **Kasus:** Bayi terinput dua kali dengan NIK kependudukan asli.
        """)
        
    with col_t2:
        st.markdown("""
        #### 🟡 Tier 2: Nama & Tgl Lahir Sama
        * **Kriteria:** Tanggal Lahir sama DAN Nama Anak persis (bebas huruf kapital/spasi).
        * **Kepastian:** **Tinggi**
        * **Kasus:** Duplikat akibat satu entri menggunakan NIK sementara (`'033...`).
        """)
        
    with col_t3:
        st.markdown("""
        #### 🟠 Tier 3: Fuzzy / Ortu + Tgl Lahir
        * **Kriteria:** Tanggal Lahir sama DAN (Nama Ortu sama ATAU Kemiripan Nama $\ge 85\%$).
        * **Kepastian:** **Perlu Verifikasi Manual**
        * **Kasus:** Singkatan nama, beda ejaan ortu (*Zulvani* vs *Zulfani*), atau nama lahir (*By Ny...*).
        """)

# --- SIDEBAR UPLOAD ---
st.sidebar.header("📂 Sumber Data")
uploaded_file = st.sidebar.file_uploader("Unggah File Excel (.xlsx)", type=["xlsx"])

if uploaded_file is not None and 'df_working' not in st.session_state:
    df_raw = pd.read_excel(uploaded_file)
    st.session_state['df_working'] = df_raw.copy()
    st.session_state['processed'] = False
    st.session_state['resolved_groups'] = set()

if 'df_working' in st.session_state:
    df_working = st.session_state['df_working']
    
    st.info(f"📁 **File Aktif:** Total Data: **{len(df_working)}** baris")
    
    if not st.session_state.get('processed', False):
        if st.button("🚀 Jalankan Deteksi Duplikat", type="primary"):
            with st.spinner("Memproses algoritma deteksi ganda Tier 1, 2, dan 3..."):
                result_df = run_detection(df_working)
                st.session_state['df_working'] = result_df
                st.session_state['processed'] = True
                st.rerun()

if st.session_state.get('processed', False):
    df_working = st.session_state['df_working']
    resolved_groups = st.session_state.get('resolved_groups', set())
    
    duplicates = df_working[df_working['duplicate_tier'].notnull()]
    active_duplicates = duplicates[~duplicates['group_id'].isin(resolved_groups)]
    
    # Navigation Tabs
    tab1, tab2 = st.tabs(["📊 Dashboard & Hasil Deteksi", "🛠️ Modul Resolusi & Merge Data"])
    
    # ---------------- TAB 1: DASHBOARD ----------------
    with tab1:
        st.subheader("Ringkasan Analisis")
        
        # Row 1: General Metrics
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Total Data", f"{len(df_working)} baris")
        col2.metric("Total Terindikasi Ganda", f"{len(duplicates)} baris")
        col3.metric("Kelompok Belum Di-review", f"{active_duplicates['group_id'].nunique()} kelompok")
        col4.metric("Kelompok Terselesaikan", f"{len(resolved_groups)} kelompok")
        
        st.markdown("---")
        
        # Row 2: Breakdown Jumlah per Tier
        st.markdown("#### 📌 Rincian Deteksi per Tingkatan (Tier)")
        
        t1_rows = len(duplicates[duplicates['duplicate_tier'].str.contains("Tier 1", na=False)])
        t1_groups = duplicates[duplicates['duplicate_tier'].str.contains("Tier 1", na=False)]['group_id'].nunique()
        
        t2_rows = len(duplicates[duplicates['duplicate_tier'].str.contains("Tier 2", na=False)])
        t2_groups = duplicates[duplicates['duplicate_tier'].str.contains("Tier 2", na=False)]['group_id'].nunique()
        
        t3_rows = len(duplicates[duplicates['duplicate_tier'].str.contains("Tier 3", na=False)])
        t3_groups = duplicates[duplicates['duplicate_tier'].str.contains("Tier 3", na=False)]['group_id'].nunique()
        
        col_m1, col_m2, col_m3 = st.columns(3)
        col_m1.metric("🟢 Tier 1 (NIK Valid)", f"{t1_groups} kelompok", delta=f"{t1_rows} baris data", delta_color="normal")
        col_m2.metric("🟡 Tier 2 (Nama & Tgl Lahir)", f"{t2_groups} kelompok", delta=f"{t2_rows} baris data", delta_color="normal")
        col_m3.metric("🟠 Tier 3 (Fuzzy / Ortu)", f"{t3_groups} kelompok", delta=f"{t3_rows} baris data", delta_color="normal")
        
        st.divider()
        
        # Filter Tier & Data Table
        tiers_available = ["Semua Tier"] + list(duplicates['duplicate_tier'].unique())
        selected_tier = st.selectbox("Saring berdasarkan Tingkatan (Tier):", tiers_available)
        
        filtered_dup = duplicates if selected_tier == "Semua Tier" else duplicates[duplicates['duplicate_tier'] == selected_tier]
        
        cols_to_display = ['group_id', 'duplicate_tier', 'ID', 'NIK Anak', 'Nama Anak', 'Tanggal Lahir Anak', 'Nama Orang Tua', 'Puskesmas']
        
        st.dataframe(
            filtered_dup[cols_to_display].sort_values(by=['group_id', 'duplicate_tier']),
            use_container_width=True,
            hide_index=True
        )

    # ---------------- TAB 2: RESOLUSI & MERGE ----------------
    with tab2:
        st.subheader("🛠️ Modul Penggabungan (Merge) & Eliminasi Duplikat")
        
        unresolved_groups = sorted(active_duplicates['group_id'].unique().tolist())
        
        if not unresolved_groups:
            st.success("🎉 Semua kelompok data ganda telah berhasil diselesaikan/di-review!")
        else:
            st.markdown("Pilih kelompok duplikat di bawah ini untuk menentukan **Data Utama (Master)** dan menggabungkan riwayat imunisasinya, atau lewati jika data terbukti bukan duplikat.")
            
            selected_group = st.selectbox("Pilih Kelompok Duplikat (Group ID):", unresolved_groups)
            
            group_data = df_working[df_working['group_id'] == selected_group].copy()
            tier_info = group_data['duplicate_tier'].iloc[0]
            
            st.info(f"**Tipe Duplikat:** `{tier_info}` | Jumlah Entri: **{len(group_data)}**")
            
            # Tampilkan Perbandingan Side-by-Side
            cols = st.columns(len(group_data))
            for idx, (index_row, row) in enumerate(group_data.iterrows()):
                with cols[idx]:
                    st.markdown(f"### Option {idx + 1}")
                    st.write(f"**ID:** {row['ID']}")
                    st.write(f"**NIK Anak:** {row['NIK Anak']}")
                    st.write(f"**Nama Anak:** {row['Nama Anak']}")
                    st.write(f"**Tgl Lahir:** {row['Tanggal Lahir Anak']}")
                    st.write(f"**Nama Ortu:** {row['Nama Orang Tua']}")
                    st.write(f"**Puskesmas:** {row['Puskesmas']}")
                    
                    # Hitung kelengkapan tanggal imunisasi
                    imun_cols = [c for c in group_data.columns if 'Tanggal Imunisasi' in c or 'Tanggal IDL' in c]
                    filled_imun = row[imun_cols].notnull().sum()
                    st.caption(f"💉 Riwayat Imunisasi Terisi: **{filled_imun} / {len(imun_cols)}**")
            
            st.divider()
            
            # Form Konfigurasi & Tombol Aksi
            st.write("#### Konfigurasi Penggabungan Data")
            master_id_options = group_data['ID'].tolist()
            selected_master_id = st.selectbox(
                "Pilih ID yang dijadikan DATA UTAMA (Master Record):",
                master_id_options,
                format_func=lambda x: f"ID: {x} - {group_data[group_data['ID']==x]['Nama Anak'].values[0]}"
            )
            
            merge_imunization = st.checkbox(
                "Otomatis gabungkan riwayat imunisasi yang kosong di Data Utama dari data duplikatnya", 
                value=True
            )
            
            st.write("")
            btn_col1, btn_col2 = st.columns([1, 1])
            
            # Tombol 1: Selesaikan & Gabung
            with btn_col1:
                if st.button("✅ Selesaikan & Gabungkan Data Kelompok Ini", type="primary", use_container_width=True):
                    master_idx = group_data[group_data['ID'] == selected_master_id].index[0]
                    duplicate_indices = group_data[group_data['ID'] != selected_master_id].index.tolist()
                    
                    # Merging logic
                    if merge_imunization:
                        for dup_idx in duplicate_indices:
                            for col in df_working.columns:
                                if pd.isna(df_working.at[master_idx, col]) and pd.notna(df_working.at[dup_idx, col]):
                                    df_working.at[master_idx, col] = df_working.at[dup_idx, col]
                    
                    # Hapus baris duplikat selain master
                    df_working = df_working.drop(index=duplicate_indices)
                    
                    # Tandai group sebagai terselesaikan
                    st.session_state['resolved_groups'].add(selected_group)
                    st.session_state['df_working'] = df_working
                    
                    st.success(f"Kelompok {selected_group} berhasil digabungkan ke ID {selected_master_id}!")
                    st.rerun()

            # Tombol 2: Lewati (Bukan Duplikat)
            with btn_col2:
                if st.button("⏭️ Lewati (Bukan Duplikat)", type="secondary", use_container_width=True):
                    st.session_state['resolved_groups'].add(selected_group)
                    st.info(f"Kelompok {selected_group} dilewati. Kedua data dipertahankan secara terpisah.")
                    st.rerun()

    # ---------------- EXPORT CLEAN DATASET ----------------
    st.sidebar.divider()
    st.sidebar.header("💾 Export Data Bersih")
    
    clean_df = df_working.drop(columns=['duplicate_tier', 'group_id'], errors='ignore')
    
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        clean_df.to_excel(writer, index=False, sheet_name='Kohort_Clean')
    processed_data = output.getvalue()
    
    st.sidebar.download_button(
        label="📥 Unduh Data Bersih (.xlsx)",
        data=processed_data,
        file_name="Data_Kohort_Bayi_Clean.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )