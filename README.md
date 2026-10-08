**Coach Wearable**

Au départ, les wearables étaient surtout utilisés par les athlètes de haut niveau. Aujourd'hui ils se démocratisent, et je pense qu'ils 
vont vite devenir de vrais outils de suivi de la santé physique et mentale pour tout le monde.

Mon bracelet connecté (un Fitbit Air) me donne plein de chiffres sur mon sommeil et ma récupération, mais rarement une idée claire 
de ce que je devrais changer. J'ai donc construit un petit outil qui, une fois par semaine, croise ces données avec un quizz 
rapide et me sort un récap avec 1 ou 2 actions concrètes par catégorie.


**Comment ça marche**

Le script récupère les 3 dernières semaines de sommeil, de HRV et de fréquence cardiaque au repos via la Google Health API.
Ensuite il pose une quinzaine de questions sur ce que la montre ne mesure pas : activité, alimentation, alcool, stress,
émotions...

Le récap est découpé en 3 parties : hygiène de vie, santé physique et santé mentale. Les messages sont rédigés par Claude, 
et si Claude ne répond pas, le script utilise des messages écrits à l'avance.

**Quelques choix que j'ai faits**

Au départ je faisais une reco tous les jours (GO / LIGHT / REST). Je suis passé à un bilan par semaine.
Les données sont comparées à ma propre moyenne des semaines précédentes, pas à des normes générales.
La note finale mélange le ressenti (65 %) et les données de la montre (35 %).
Tout ce qui est sensible est géré par des règles fixes et pas par l'IA. Par exemple, l'alerte cardio ne se déclenche que 
si la FC au repos est haute au moins 3 jours dans la semaine, pour éviter de paniquer après une mauvaise nuit.

**Où j'en suis**

Le prototype tourne sur mes propres données pour l'instant.

Prochaines étapes : parler à d'autres utilisateurs de wearables, regarder de plus près Whoop, Oura et Google Health Coach, 
puis lancer un test de 2 semaines avec 8 à 10 personnes pour voir si elles appliquent vraiment les actions proposées.

**Lancer le projet**

bash
pip install -r requirements.txt
cp .env.example .env   # puis remplir vos identifiants Google Health API + clés API Claude 
python3 coach.py

Claude Code est optionnel : sans lui, le récap fonctionne avec les messages par défaut. Les réponses au quiz restent en local dans data/.

Ce n'est pas un outil médical, juste un projet perso.

_Auteur_ : Sivan Mootoosamy 
