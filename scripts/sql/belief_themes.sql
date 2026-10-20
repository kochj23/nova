-- belief_themes: "The Big Question" topics for Belief and Friends theme days (Jordan 2026-10-19: "an overall story
-- ... with supporting and disagreeing viewpoints of the various religions. Not all the time but about half the
-- time. Pick a random topic and go from there."). nova_friends picks randomly among themes not used in 30 days,
-- least-recently-used first; keywords find the material (Postgres regex alternatives, matched case-insensitively
-- on whole words against video transcripts and the scripture / non-theism libraries).
BEGIN;
CREATE TABLE IF NOT EXISTS belief_themes (
    theme      text PRIMARY KEY,
    question   text NOT NULL,
    keywords   text[] NOT NULL CHECK (cardinality(keywords) >= 3),
    last_used  date,
    times_used int NOT NULL DEFAULT 0,
    active     boolean NOT NULL DEFAULT true,
    added_at   timestamptz NOT NULL DEFAULT now()
);
INSERT INTO belief_themes (theme, question, keywords) VALUES
 ('afterlife', 'What Happens When We Die?', '{afterlife,"life after death",heaven,hell,paradise,rebirth,reincarnation,resurrection,soul,judgment day,barzakh,bardo,nirvana}'),
 ('suffering', 'Why Is There Suffering?', '{suffering,evil,theodicy,dukkha,pain,affliction,"problem of evil",karma,trial,test}'),
 ('fasting', 'Why Do the Faithful Go Hungry? Fasting Across Traditions', '{fasting,fast,ramadan,lent,"yom kippur",abstinence,"great lent",paryushana,ekadashi,sawm}'),
 ('forgiveness', 'Can Anything Be Forgiven?', '{forgiveness,forgive,repentance,teshuvah,tawbah,confession,mercy,atonement,pardon}'),
 ('creation', 'How Did It All Begin? Creation Stories', '{creation,genesis,cosmogony,"in the beginning",big bang,creator,brahma,primordial,cosmos,origin}'),
 ('end-times', 'How Does the World End?', '{apocalypse,"end times","end of the world",eschatology,messiah,mahdi,judgment,armageddon,kali yuga,maitreya,rapture}'),
 ('salvation', 'Saved From What? Sin, Salvation and Liberation', '{salvation,sin,grace,liberation,moksha,nirvana,enlightenment,redemption,"saved",mukti}'),
 ('pilgrimage', 'Why Travel to a Holy Place? Pilgrimage', '{pilgrimage,pilgrim,hajj,umrah,jerusalem,mecca,varanasi,kumbh,camino,lourdes,bodh gaya,karbala}'),
 ('food-rules', 'You Are What You Eat: Sacred Food Rules', '{kosher,halal,vegetarian,dietary,pork,meat,"food laws",sattvic,ahimsa,kashrut}'),
 ('images-idols', 'Icons or Idols? Images of the Sacred', '{icon,icons,idol,idols,idolatry,images,iconoclasm,murti,statue,aniconism,depiction}'),
 ('prayer-meditation', 'Talking to God or Quieting the Mind? Prayer and Meditation', '{prayer,pray,meditation,meditate,salat,mantra,contemplation,mindfulness,dhikr,rosary,liturgy}'),
 ('wealth-charity', 'Camels and Needles: Wealth and Charity', '{charity,wealth,zakat,tithe,tzedakah,poverty,rich,money,dana,almsgiving,greed}'),
 ('women', 'Who May Lead? Women''s Roles in Religion', '{women,woman,ordination,priestess,nuns,female,gender,hijab,"women''s ordination",rabbas,bhikkhuni}'),
 ('marriage-sexuality', 'Marriage, Sex and the Sacred', '{marriage,sexuality,celibacy,divorce,chastity,polygamy,wedding,"same-sex",modesty,purity}'),
 ('violence', 'Holy War or Holy Peace? Violence and Just War', '{violence,"just war",jihad,crusade,pacifism,nonviolence,ahimsa,war,martyrdom,peace}'),
 ('body', 'The Body: Resurrection or Reincarnation?', '{body,resurrection,reincarnation,transmigration,flesh,embodiment,cremation,burial,incarnation,rebirth}'),
 ('miracles', 'Do Miracles Happen?', '{miracle,miracles,supernatural,healing,wonders,signs,karamat,siddhis,apparition,"miraculous"}'),
 ('doubt-apostasy', 'Doubt, Leaving and Apostasy', '{doubt,apostasy,apostate,leaving,"faith crisis",deconversion,ex-mormon,ex-muslim,disaffiliation,unbelief,skepticism}'),
 ('scripture-authority', 'Whose Word Is It? Scripture and Its Authority', '{scripture,revelation,inspired,inerrancy,canon,torah,quran,vedas,authority,"word of god",interpretation}'),
 ('prophets-founders', 'Prophets, Founders and Teachers', '{prophet,prophets,founder,messenger,guru,buddha,moses,muhammad,jesus,"joseph smith",teacher}'),
 ('spirits', 'Angels, Demons and Spirits', '{angel,angels,demon,demons,jinn,spirits,devas,satan,possession,exorcism,ghosts}'),
 ('heaven-hell', 'Heaven, Hell or Neither?', '{heaven,hell,paradise,purgatory,jannah,jahannam,gehenna,naraka,"pure land",damnation}'),
 ('law-conscience', 'Religious Law or Conscience?', '{law,sharia,halakha,commandments,conscience,canon law,obedience,fatwa,ethics,"moral law"}'),
 ('science', 'Science and Religion: Conflict or Conversation?', '{science,evolution,darwin,cosmology,galileo,scientific,"big bang",creationism,naturalism,experiment}'),
 ('death-rituals', 'How We Bury Our Dead', '{funeral,burial,cremation,mourning,shiva,"death rites",grave,janazah,"last rites",ancestors}'),
 ('holy-days', 'Holy Days and the Sabbath', '{sabbath,shabbat,holiday,festival,easter,passover,diwali,eid,vesak,"holy day",jumu''ah}'),
 ('conversion', 'Why Do People Convert?', '{conversion,convert,converts,baptism,shahada,proselytize,missionary,initiation,"born again",dawah}'),
 ('pluralism', 'Who Gets Saved? Pluralism and Exclusivism', '{pluralism,exclusivism,"other religions",universalism,interfaith,salvation,"only way",inclusivism,unbelievers,tolerance}'),
 ('animals', 'Do Animals Have Souls?', '{animals,animal,ahimsa,sacrifice,cow,vegetarian,"animal welfare",creatures,"souls of animals",slaughter}'),
 ('environment', 'Who Owns the Earth? Religion and the Environment', '{environment,creation care,ecology,climate,nature,stewardship,earth,"laudato si",sustainability,trees}'),
 ('free-will', 'Fate or Free Will?', '{"free will",predestination,fate,destiny,qadar,karma,determinism,providence,choice,sovereignty}'),
 ('god-nature', 'One God, Many Gods or None?', '{monotheism,polytheism,trinity,tawhid,brahman,atheism,godhead,deity,deities,nontheism,"oneness of god"}'),
 ('meaning', 'What Is the Meaning of Life?', '{meaning,purpose,"meaning of life",dharma,vocation,happiness,flourishing,nihilism,existential,calling}'),
 ('evil-satan', 'Where Does Evil Come From?', '{evil,satan,devil,iblis,mara,"original sin",temptation,ahriman,demons,wickedness}'),
 ('authority-clergy', 'Who Speaks for God? Clergy and Authority', '{priest,clergy,pope,imam,rabbi,bishop,guru,authority,hierarchy,ordination,prophet}'),
 ('mysticism', 'Mystics: Meeting the Divine Directly', '{mysticism,mystic,mystical,sufi,kabbalah,contemplative,union,ecstasy,"inner light",gnosis}'),
 ('community', 'Belonging: Congregation, Sangha and Ummah', '{community,congregation,sangha,ummah,church,synagogue,mosque,belonging,fellowship,kehillah}'),
 ('modernity', 'Old Faiths in a Modern World', '{modernity,modern,secular,secularization,reform,tradition,technology,internet,"social media",change}'),
 ('children-education', 'Raising Children in a Faith', '{children,parenting,education,"religious education",catechism,"sunday school",madrasa,bar mitzvah,upbringing,youth}'),
 ('politics', 'Church, State and Power', '{politics,government,state,"religious liberty","separation of church and state",theocracy,secular,law,election,freedom}')
ON CONFLICT (theme) DO NOTHING;
COMMIT;
