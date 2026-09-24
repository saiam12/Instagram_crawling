"""Gemini Reel 분석에 사용하는 프롬프트."""


ANALYSIS_PROMPT = """
이 Instagram Reel 영상을 분석하세요.

주어진 JSON 스키마에 맞춰 결과를 반환해야 합니다. 아래 지침을 반드시 지키세요.
분석 목적은 원본의 인물/캐릭터 외형, 상품 외형, 움직임과 인물·의상·추천 대상의 관계를 기록하는 것입니다.
기존 JSON 필드 안에 상세 내용을 기록하고, 스키마에 없는 필드는 추가하지 마세요.
보이지 않는 부분이나 흐려서 확인할 수 없는 특징은 추측하지 말고 '확인 불가'로 표시하세요.
브랜드, 제품명, 인물의 신원은 추정하지 마세요. 시간과 위치는 관찰 가능한 범위에서만 기술하세요.
모든 설명은 한국어로 작성하되, generation_prompts.video_prompt_en만 영어로 작성하세요.

[분석 절차]
1. 출력을 작성하기 전에 영상을 처음부터 끝까지 확인하고, 실제 영상 길이와 모든 시각적·청각적 전환 시점을 먼저 파악하세요.
2. 커트, 구도, 대상, 의상/상품, 텍스트가 바뀌는 순간을 기준으로 신 경계를 먼저 확정하세요.
3. 확정한 타임라인을 기준으로 scene_details를 완성한 뒤, 요약 필드와 generation_prompts를 작성하세요.
4. visual_description은 화면에서 확인한 사실만 적고, emotional_tone, purpose, marketing_analysis에서만 근거 있는 해석을 제공하세요.

[인물/캐릭터 외형 기록]
1. subjects.main_subject에 주요 인물/캐릭터 각각을 구별할 수 있는 이름표(인물 A, 캐릭터 A 등)를 붙이고,
   얼굴 윤곽, 눈/눈썹/코/입의 보이는 형태, 헤어스타일과 색, 체형과 신체 비율을 구체적으로 적으세요.
   인형 탈이나 마스코트라면 실제 사람의 얼굴로 해석하지 말고 머리 형태, 귀, 눈/입의 배치,
   털/천/플라스틱 등 보이는 표면 질감과 색 구획을 기록하세요.
2. 의상은 상의/하의/신발/액세서리별 색, 핏, 길이, 소재의 시각적 특징, 무늬와 장식 위치를 기록하세요.
   여러 인물이 있으면 각 인물의 특징을 섞지 말고 장면마다 동일한 이름표를 사용하세요.
3. scene_details의 visual_description에도 해당 장면에서 보이는 외형, 의상, 표정, 시선 방향을 기록하세요.
   장면 사이 의상이나 외형이 실제로 달라지면 그 변화도 명시하세요.
4. subjects.people에는 실제 등장 인물을 인물 A, 인물 B처럼 한 번씩 기록하고, main_subject와 모든 장면에서 같은 이름표를 사용하세요.
   gender_presentation은 화면에서 확인되는 성별 표현만 male | female | unknown으로 기록합니다.
   인물의 실제 성별·성 정체성이나 정확한 나이는 추정하지 마세요. 얼굴이 가려지거나 표현이 모호하면 unknown입니다.
   의상 종류만으로 성별 표현을 결정하지 말고, 사람이 아닌 캐릭터·상품은 사람으로 기록하지 마세요.

[장면별 착용자와 의상]
1. scene_details.worn_outfits에는 그 장면에서 실제로 옷을 입고 있는 인물별로 항목을 하나씩 작성하세요.
   subject_id는 subjects.people의 이름표와 정확히 같고, gender_presentation도 해당 인물의 코드와 같아야 합니다.
2. clothing_items에는 실제로 착용한 상의, 하의, 겉옷, 신발, 액세서리를 각각 구분해 기록하세요.
   상품 A처럼 product.description에 이름표를 붙인 상품이라면 같은 이름표를 함께 적으세요.
   여러 사람이 동시에 등장하면 사람별 항목을 분리하고, 착장이 바뀌는 순간에는 장면을 나누세요.
3. 플랫레이, 마네킹, 손에 든 옷처럼 실제 착용자가 보이지 않는 장면은 worn_outfits=[]입니다.
   그런 옷은 product.description과 visual_description에만 기록하고 착용자나 성별을 만들어내지 마세요.

[상품 외형과 배치 기록]
1. product.description에 상품 A, 상품 B처럼 개별 상품을 구분해 기록하세요. 코디 세트도 개별 품목으로 나누고,
   각 상품의 종류, 실루엣, 가로/세로 비율, 색 구획, 재질과 표면 질감, 패턴, 부속품을 설명하세요.
   로고/문구는 읽히는 경우만 원문과 위치를 적고, 읽히지 않으면 판독 불가로 표시하세요.
2. visual_description에는 해당 상품의 화면 내 위치, 다른 물체 대비 크기, 방향, 앞/뒷면 노출,
   겹침과 가림 상태를 기록하세요. 실측 크기를 추정하지 마세요.
3. 플랫레이 장면은 상의/하의/신발/소품의 상대적 배치와 간격, 배경색, 그림자 방향까지 설명하세요.
   정지된 상품 배치를 사람이 착용하거나 손으로 움직이는 장면으로 해석하지 마세요.

[움직임 기록]
1. 각 visual_description에 '시작 상태 / 시간별 동작 / 종료 상태' 순서로 자세와 움직임을 기록하세요.
   영상 전체 기준 초 단위 시점을 사용하고, 동작 변화가 보이는 시점만 구분하세요.
2. 어느 신체 부위가 무엇을 잡고 어디에서 어디로 움직이는지, 이동 방향, 회전, 속도 변화,
   멈춤과 놓는 순간을 기술하세요. 좌우는 화면 기준임을 명시하고, 실제 오른손/왼손은 확실할 때만 적으세요.
   거울 셀카의 좌우 반전을 고려하고, 가려진 동작이나 샘플 사이 동작을 만들어내지 마세요.
3. 피사체 움직임은 visual_description에, 카메라 이동은 camera_movement에 분리해 기록하세요.
   정지 장면은 '움직임 없음'으로 명시하고, 컷으로 상품이 바뀐 것을 물체가 변형되거나 이동한 것으로 쓰지 마세요.

[오디오 분석]
1. 영상에서 실제로 들리는 소리만 분석하고, 음성이 불분명한 부분을 문맥으로 추측하지 마세요.
2. audio_analysis.transcript에는 말로 발화된 대사와 내레이션만 시간순으로 기록하세요.
   노래 가사는 전사하지 말고 background_music.vocals에 보컬 존재 여부만 기록하세요.
3. transcript의 각 항목은 start_second, end_second, speaker, text 순서로 작성하세요.
   화자는 화면의 인물 이름표와 동일한 인물 A, 인물 B를 사용하고, 내레이션은 narrator, 식별할 수 없으면 unknown을 사용하세요.
   text는 들리는 원문 그대로 적고 일부를 알아들을 수 없으면 해당 부분만 [판독 불가]로 표시하세요.
4. sound_events에는 대사와 BGM을 제외한 비언어 소리를 시간순으로 기록하세요. type은
   laughter | clap | snap | footsteps | impact | whoosh | click | object_handling | animal | vehicle | ambient | other 중 하나만 사용하세요.
5. background_music.mood는 neutral | upbeat | calm | dark | dramatic | romantic | playful | energetic | sad | other | unknown,
   tempo는 none | slow | medium | fast | variable | unknown 중 하나만 사용하세요.
6. 음성이 없으면 speech_present=false, language=null, transcript=[]로 반환하세요.
   BGM이 없으면 background_music은 exists=false, mood=null, tempo=none, vocals=null로 반환하세요.

[썸네일/대표 화면 분석]
1. INSTAGRAM_COVER_IMAGE_START/END 또는 VIDEO_N_INSTAGRAM_COVER_START/END 사이에 별도 이미지가 제공되면
   해당 VIDEO_N과 같은 번호의 이미지만 그 영상의 썸네일로 분석하고
   thumbnail_analysis.source=provided_cover_image로 반환하세요.
2. 별도 이미지가 없으면 영상의 첫 번째 실제 프레임을 대표 화면으로 사용하고
   thumbnail_analysis.source=video_first_frame으로 반환하세요. Instagram에서 지정한 커버라고 추정하지 마세요.
3. visual_description에는 화면에서 직접 보이는 인물, 상품, 배경, 색상과 배치를 기록하고,
   on_screen_text에는 판독 가능한 원문만 적으세요. focal_point와 composition은 시선 집중 요소와 구도 근거를 설명하세요.
4. selling_point는 대표 화면만으로 명확히 전달되는 경우에만 기록하고 그렇지 않으면 null을 사용하세요.
5. strengths, weaknesses, effectiveness는 가독성, 핵심 대상의 명확성, 대비, 영상 내용과의 일치 여부를 근거로 판단하세요.
   evidence_confidence는 클릭률 예측값이 아니라 대표 화면에서 판단 근거를 직접 확인할 수 있는 정도입니다.

[구조 판별 지침]
1. 영상이 인물/캐릭터가 등장하는 훅(hook) 이후, 인물 없이 상품이나 코디를 배치한
   플랫레이(flat lay) 이미지/영상 전환 구조로 바뀌는지 반드시 확인하고 body_structure에 명시하세요.
   훅 구간과 바디 구간의 시각적 구성(등장 인물 유무, 구도, 배경)이 다르다면 그 차이를 구체적으로 설명하세요.
2. 영상의 0초 이상 3초 미만은 hook, 3초 이상은 body로 분류하세요. 3초를 가로지르는 장면은 3초 경계에서 나누고,
   영상이 3초보다 짧으면 모든 장면을 hook으로 분류하세요.

[씬 분석 지침]
1. 컷이 바뀌거나(화면 전환), 카메라 구도가 바뀌거나, 인물 유무가 바뀌거나, 새로운 텍스트/자막이
   등장하는 시점마다 반드시 새로운 scene_detail 항목을 만드세요. 뭉뚱그려서 크게 나누지 마세요.
2. scene_number는 1부터 시작해 시간순으로 1씩 증가해야 하며, 모든 장면은 start_second < end_second여야 합니다.
3. 첫 장면은 start_second=0으로 시작하고, 다음 장면의 start_second는 바로 앞 장면의 end_second와 같아야 합니다.
   빈 구간이나 겹치는 구간을 만들지 마세요. 마지막 장면의 end_second는 실제 영상 종료 시각과 일치해야 합니다.
4. 하나의 시각적 상태가 3초를 넘게 유지되면 동작의 자연스러운 하위 단계를 기준으로 3초 이하의 연속 장면으로 나누세요.
   이 분할은 새로운 커트를 의미하지 않으며, transition_in에 임의의 전환 효과를 만들지 마세요.
5. layout_type에는 mirror_selfie, flat_lay_outfit_grid, closeup, talking_head, product_shot 등
   실제 관찰되는 형태를 최대한 정확한 표현으로 적으세요.
6. visual_description은 "사람이 말한다" 같은 뭉뚱그린 표현 대신, 무엇을 어떻게 하고 있는지 구체적으로 서술하세요.
7. on_screen_text에는 화면에서 확실히 판독되는 텍스트만 원문 그대로 옮기세요. 일부만 판독되면 확인된 부분과 '[판독 불가]'를 구분하고,
   텍스트가 없거나 전혀 판독할 수 없으면 null을 사용하세요.

[필드 간 일관성]
1. subjects.people_count는 영상 전체에서 구별되는 실제 인물의 수입니다. 거울이나 반사에 나타난 같은 인물을 두 명으로 세지 마세요.
2. subtitle.exists는 판독 여부와 관계없이 텍스트 오버레이가 한 번이라도 보이면 true입니다. false인 경우 position과 style은 null이어야 합니다.
3. camera, editing, subjects, product, body_structure는 scene_details에 기록한 사실을 요약해야 하며, scene_details에 없는 대상이나 전환을 추가하지 마세요.
4. product.exists가 false면 description은 null이어야 하며, true면 실제로 보이는 상품만 description에 포함하세요.
5. marketing_analysis의 강점·약점·특이 요소와 selling_points는 관찰된 훅, 편집, 상품 노출, 대사, 자막, CTA에
   근거해야 하며 성과나 시청자 반응을 지어내지 마세요.
6. scene_details.worn_outfits의 착용자 이름표와 성별 표현은 subjects.people과 일치해야 합니다.

[추천 대상 분석]
1. recommended_audience는 실제 시청자 통계가 아니라 영상이 제안하는 옷·스타일의 추천 대상입니다.
   화면 문구, 들리는 대사, 상품과 스타일의 제시 방식 등 영상에서 확인한 근거를 evidence에 적으세요.
2. age_group은 10s | 20s | 30s | 20s_30s | 40s_plus | all | unknown 중 하나입니다.
   '20대'처럼 연령이 명시되거나 명확한 맥락이 있을 때만 20s로 좁히세요.
   정확히 20대인지 30대인지 좁히기 어렵지만 성인 20~30대에게 추천할 만한 영상 근거가 있으면
   20s_30s를 쓰고, 연령 근거가 부족하면 unknown을 쓰세요.
3. gender는 male | female | all | unknown 중 하나입니다. 남성과 여성 대상이 각각 확인되면
   같은 연령대라도 항목을 나눠 '20대 남성', '20대 여성'처럼 기록하세요.
   성별 구분 없이 모두에게 추천하는 근거가 있으면 all을 사용하고, 중복 항목은 만들지 마세요.
   등장 인물의 성별 표현만으로 추천 대상의 성별을 단정하지 마세요.
4. 연령과 성별을 모두 판단할 근거가 없으면 recommended_audience=[]로 반환하세요.

[판매 포인트 분석]
1. marketing_analysis.selling_points에는 영상이 상품이나 콘텐츠의 매력으로 실제 강조한 요소만 기록하세요.
2. 각 항목은 point, evidence, spoken_evidence, visual_evidence, on_screen_text_evidence,
   start_second, end_second, appeal_type, evidence_confidence 순서로 작성하세요.
3. appeal_type은 product_feature | product_variety | styling_inspiration | transformation | convenience |
   price_value | scarcity | social_proof | aspiration | novelty | brand_identity | other 중 하나만 사용하세요.
4. evidence에는 판단 근거를 종합해 설명하고, 근거 종류를 다음 필드에 분리하세요.
   - spoken_evidence: 실제 transcript에서 들리는 핵심 대사 원문. 음성 근거가 없으면 null
   - visual_evidence: 같은 시간대에 직접 보이는 행동, 상품 노출, 시연 또는 전후 변화. 없으면 null
   - on_screen_text_evidence: 같은 시간대에 판독되는 자막·가격·CTA 등의 원문. 없으면 null
   대사나 문구를 의역해서 원문처럼 만들지 말고, 각 근거가 존재하는 전체 범위를 start_second와 end_second로 기록하세요.
5. evidence_confidence는 판매 성과나 구매 전환 확률이 아니라, 관찰된 근거가 해당 판매 포인트 해석을
   얼마나 명확하게 지지하는지 나타내는 0~1 값입니다. 0.9 이상은 직접적이고 명확한 근거,
   0.7 이상은 강한 간접 근거, 0.5 이상은 제한적인 근거이며 0.5 미만이면 항목을 만들지 마세요.
6. 영상에서 판매 포인트를 확인할 수 없으면 selling_points=[]로 반환하세요.

[필드별 출력 규격]
아래 필드를 하나도 생략하지 말고 매번 같은 키와 자료형으로 반환하세요. 스키마에 없는 키는 추가하지 마세요.
문자열 필드에서 확인할 수 없는 내용은 "확인 불가", 해당 사항이 없는 문자열은 "없음"으로 적으세요.
Optional 필드에 해당 사항이 없으면 null, 목록에 항목이 없으면 [], boolean은 false를 사용하세요.
- summary: 영상 전체의 대상, 행동, 배경, 전개를 한 문단으로 요약한 문자열
- hook: description(0~3초의 시각·텍스트·행동 훅), strength(strong | medium | weak)
- body_structure: 3초 이후 장면의 시간순 전개와 훅 대비 변화(인물 유무, 구도, 배경)를 설명한 문자열
- camera: main_composition(대표 구도), angles(관찰된 앵글 문자열 목록), movements(관찰된 카메라 이동 문자열 목록)
- editing: style(대표 편집 방식), cut_speed(fast | medium | slow), transitions(실제로 관찰된 전환 기법 목록)
- subtitle: exists(텍스트 오버레이 존재 여부), position(대표 화면 위치 또는 null), style(글자 형태·색·배경·효과 또는 null)
- subjects: people_count(영상 전체에서 구별되는 실제 인물 수), main_subject(인물/캐릭터별 이름표, 외형, 의상),
  people(각 실제 인물의 subject_id, gender_presentation)
- product: exists(상품 노출 여부), description(상품별 이름표, 종류, 외형, 재질, 패턴, 부속품 또는 null)
- scene_details: 0초부터 종료까지 빈틈없이 이어지는 장면 목록. 각 항목은 scene_number, start_second, end_second,
  section(hook | body), visual_description, worn_outfits(각 착용자의 subject_id, gender_presentation, clothing_items),
  layout_type, camera, camera_movement, on_screen_text, audio_or_dialogue,
  transition_in, emotional_tone, purpose를 모두 포함
- audio_analysis: speech_present, language, transcript, sound_events, background_music를 모두 포함.
  transcript 항목은 start_second, end_second, speaker, text 순서이고, sound_events 항목은
  start_second, end_second, type, description 순서로 작성
- content_type: 영상의 대표 형식을 나타내는 짧은 문자열(예: lookbook, product_demo, talking_head)
- thumbnail_analysis: source, visual_description, on_screen_text, focal_point, composition, selling_point,
  strengths, weaknesses, effectiveness, evidence_confidence를 모두 포함
- marketing_analysis: strengths, weaknesses, notable_elements, selling_points를 모두 포함. selling_points의 각 항목은
  point, evidence, spoken_evidence, visual_evidence, on_screen_text_evidence,
  start_second, end_second, appeal_type, evidence_confidence 순서로 반환
- recommended_audience: 추천 대상별 age_group, gender, evidence 목록. 근거가 없으면 []
- generation_prompts: video_prompt_en, graphic_post_processing_needed, post_processing_notes를 모두 포함

[scene_details 표준 표기]
각 scene_detail의 키는 위에 적힌 순서로 출력하고, 아래 필드는 지정된 순서와 영문 코드만 사용하세요.
관찰할 수 없는 값은 unknown을 사용하고, 비슷한 새 표현을 임의로 만들지 마세요.
- camera: 반드시 "shot_size=<값>; angle=<값>; composition=<값>" 형식으로 작성
  - shot_size: extreme_close_up | close_up | medium_close_up | medium | medium_full | full | wide | extreme_wide | insert | unknown
  - angle: eye_level | high_angle | low_angle | overhead | dutch_angle | pov | over_the_shoulder | unknown
  - composition: center | left | right | symmetrical | rule_of_thirds | flat_lay | mirror_selfie | other | unknown
- camera_movement: 반드시 "type=<값>; direction=<값>; speed=<값>" 형식으로 작성
  - type: static | handheld | pan | tilt | zoom | dolly | truck | pedestal | orbit | tracking | rack_focus | other | unknown
  - direction: none | left | right | up | down | in | out | clockwise | counterclockwise | mixed | unknown
  - speed: none | slow | medium | fast | variable | unknown
  - 카메라가 움직이지 않으면 정확히 "type=static; direction=none; speed=none"으로 작성
  - 한 장면 안에서 이동 유형이 바뀌면 유형별로 장면을 나누고 각 장면에는 주된 이동 하나만 기록
- layout_type: mirror_selfie | flat_lay_outfit_grid | closeup | talking_head | product_shot | full_body_fashion_shot |
  split_screen | text_only | scenery | other 중 하나만 사용
- transition_in: none | hard_cut | jump_cut | match_cut | dissolve | fade_in | fade_out | wipe | whip_pan |
  graphic_match | other 중 하나만 사용
- 상위 camera.angles와 camera.movements에도 각각 위 angle, camera_movement의 type 코드를 중복 없이 사용하세요.

[영상 생성 프롬프트(generation_prompts) 지침]
1. video_prompt_en은 하나의 영문 문자열 안에 줄바꿈으로 "Appearance and setting", "Shot timeline",
   "Continuity constraints" 세 구간을 반드시 같은 순서로 포함하세요.
2. Appearance and setting에는 관찰된 인물/캐릭터와 상품의 구별되는 외형, 배경, 조명을 적으세요.
3. Shot timeline에는 모든 scene_details를 순서대로 반영해 시작/종료 초, 등장 대상, 구도, 상품 배치,
   시작 자세, 동작 순서와 속도, 종료 자세, 카메라 움직임, 전환 기법을 적으세요.
4. Continuity constraints에는 원본에서 바뀌지 않는 얼굴/캐릭터 형태, 의상, 상품 외형, 배경, 카메라 조건과
   추가로 만들어내면 안 되는 인물·상품·동작을 적으세요.
5. 텍스트, 로고, 말풍선, 누끼 합성처럼 생성 후 별도 합성이 필요한 요소는 video_prompt_en에서 제외하고
   graphic_post_processing_needed를 true로, post_processing_notes에 대상·원문·위치·등장 시간을 적으세요.
   별도 합성이 필요 없으면 false와 null을 사용하세요.
6. 짧은 요약으로 축약하거나 관찰하지 않은 외형, 조명, 동작을 추가하지 마세요.

[출력 전 자체 검증]
출력 직전에 다음을 내부적으로 검사하고, 어긋나는 항목은 수정한 뒤 JSON만 반환하세요.
- 위 출력 규격의 모든 키가 존재하고 자료형과 빈값 표현이 일치하는가?
- scene_number가 1부터 연속적인가?
- 모든 장면이 start_second < end_second이고, 0초부터 실제 종료 시각까지 빈틈이나 겹침 없이 연결되는가?
- 3초 경계에서 hook과 body가 올바르게 나뉘었는가?
- 요약 필드, scene_details, video_prompt_en 사이에 인물·상품·의상·동작·시간의 모순이 없는가?
- 모든 worn_outfits의 subject_id와 gender_presentation이 subjects.people과 일치하고, 입지 않은 옷을 착용으로 기록하지 않았는가?
- recommended_audience의 연령·성별에 영상 근거가 있고 실제 시청자 통계처럼 표현하지 않았는가?
- transcript와 sound_events의 시간이 실제 영상 범위 안에 있고 시작 시각 순으로 정렬되었는가?
- transcript에 노래 가사를 포함하거나 들리지 않는 말을 추측하지 않았는가?
- selling_points마다 실제 영상 구간과 구체적인 evidence가 있으며 evidence_confidence를 판매 성과로 해석하지 않았는가?
- selling_points의 음성·화면·문구 근거가 transcript와 scene_details의 같은 시간 구간에서 실제로 확인되는가?
- thumbnail_analysis가 별도 커버 이미지 또는 첫 프레임 중 실제 제공된 소스만 분석했는가?
- 확인하지 못한 신원, 브랜드, 텍스트, 신체 특징, 동작을 추정하지 않았는가?
"""
